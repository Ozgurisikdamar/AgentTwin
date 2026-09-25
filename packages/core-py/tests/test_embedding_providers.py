"""The OpenAI-compatible embedding provider against fake ``/embeddings``
servers: what it sends, how it batches, retries and refuses, and that every
answer is checked before a vector is used (ADR-0014)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from agenttwin_core.config import Loader
from agenttwin_core.embeddings import (
    MAX_TEXT_CHARS,
    EmbeddingError,
    EmbeddingSettings,
    HashingEmbedder,
    OpenAICompatibleEmbedder,
    build_embedder,
    load_embedding_settings,
)

pytestmark = pytest.mark.anyio

DIMS = 4
SETTINGS = EmbeddingSettings(
    provider="openai_compatible",
    model="text-embedding-3-small",
    dims=DIMS,
    base_url="https://embeddings.example/v1",
    api_key="sk-test-key-0123456789",
    max_retries=2,
    batch_size=2,
)


def vector_of(text: str) -> list[float]:
    """A deterministic 4-dimensional "embedding" of a text (its length and
    first letters), so that each answer can be matched to its input."""
    return [float(len(text)), float(ord(text[0])), float(ord(text[-1])), 1.0]


class FakeProvider:
    """An ``/embeddings`` endpoint that answers in shuffled order, and
    ``script`` answers first (status, body, headers) when set."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.script: list[tuple[int, Any, dict[str, str]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        self.headers.append(dict(request.headers))
        if self.script:
            status, payload, headers = self.script.pop(0)
            if isinstance(payload, bytes):
                return httpx.Response(status, content=payload, headers=headers)
            return httpx.Response(status, json=payload, headers=headers)
        data = [
            {"object": "embedding", "index": i, "embedding": vector_of(t)}
            for i, t in enumerate(body["input"])
        ]
        return httpx.Response(
            200, json={"object": "list", "data": list(reversed(data)), "model": body["model"]}
        )


def embedder(fake: FakeProvider, **over: Any) -> tuple[OpenAICompatibleEmbedder, list[float]]:
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    settings = EmbeddingSettings(**{**SETTINGS.__dict__, **over})
    return OpenAICompatibleEmbedder(settings, transport=httpx.MockTransport(fake), sleep=sleep), slept


async def test_texts_are_embedded_in_batches_in_their_order() -> None:
    fake = FakeProvider()
    e, _ = embedder(fake)
    texts = ["refund policy", "", "lookup order", "   ", "cross tenant", "x" * (MAX_TEXT_CHARS + 50)]
    vectors = await e.embed(texts)
    # Blank texts are not sent: their vector is zero, as with the hashing model.
    sent = [r["input"] for r in fake.requests]
    assert sent == [["refund policy", "lookup order"], ["cross tenant", "x" * MAX_TEXT_CHARS]]
    assert vectors == [
        vector_of("refund policy"),
        [0.0] * DIMS,
        vector_of("lookup order"),
        [0.0] * DIMS,
        vector_of("cross tenant"),
        vector_of("x" * MAX_TEXT_CHARS),
    ]
    assert fake.requests[0] == {
        "model": "text-embedding-3-small",
        "input": ["refund policy", "lookup order"],
        "encoding_format": "float",
    }
    assert fake.headers[0]["authorization"] == "Bearer sk-test-key-0123456789"
    assert (e.model, e.dims) == ("text-embedding-3-small", DIMS)
    assert await e.embed([]) == [] and len(fake.requests) == 2
    # The key never appears in a representation.
    assert "sk-test" not in repr(e) and "sk-test" not in repr(e.settings)
    await e.close()


async def test_dimensions_are_sent_only_when_configured_and_the_key_only_when_set() -> None:
    fake = FakeProvider()
    e, _ = embedder(fake, send_dimensions=True, api_key="")
    await e.embed(["refund"])
    assert fake.requests[0]["dimensions"] == DIMS
    assert "authorization" not in fake.headers[0]


async def test_a_busy_provider_is_retried_with_its_retry_after() -> None:
    fake = FakeProvider()
    fake.script = [
        (429, {"error": {"message": "slow down"}}, {"retry-after": "3"}),
        (503, {"error": {"message": "overloaded"}}, {"retry-after": "3600"}),
    ]
    e, slept = embedder(fake)
    assert await e.embed(["refund"]) == [vector_of("refund")]
    # Retry-After is honored, but never beyond 10 seconds.
    assert slept == [3.0, 10.0] and len(fake.requests) == 3


@pytest.mark.parametrize(
    ("script", "kind", "attempts"),
    [
        ([(500, {}, {})] * 3, "unavailable", 3),
        ([(429, {}, {"retry-after": "soon"})] * 3, "rate_limited", 3),
        ([(400, {"error": {"message": "bad input"}}, {})], "rejected", 1),
        ([(401, {"error": {"message": "bad key"}}, {})], "rejected", 1),
        # A redirect is not followed: the key goes to the configured URL only.
        ([(307, {}, {"location": "https://elsewhere.example/v1/embeddings"})], "rejected", 1),
    ],
)
async def test_failures_say_what_went_wrong(
    script: list[tuple[int, Any, dict[str, str]]], kind: str, attempts: int
) -> None:
    fake = FakeProvider()
    fake.script = list(script)
    e, slept = embedder(fake)
    with pytest.raises(EmbeddingError) as err:
        await e.embed(["refund"])
    assert err.value.kind == kind and err.value.retryable is (kind != "rejected")
    assert len(fake.requests) == attempts and len(slept) == attempts - 1


async def test_an_unreachable_or_slow_provider_is_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("refused", request=request)
        raise httpx.ReadTimeout("slow", request=request)

    async def sleep(s: float) -> None:
        pass

    e = OpenAICompatibleEmbedder(SETTINGS, transport=httpx.MockTransport(handler), sleep=sleep)
    with pytest.raises(EmbeddingError) as err:
        await e.embed(["refund"])
    assert (err.value.kind, err.value.retryable, calls) == ("timeout", True, 3)
    e = OpenAICompatibleEmbedder(
        EmbeddingSettings(**{**SETTINGS.__dict__, "max_retries": 0}),
        transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x", request=r))),
    )
    with pytest.raises(EmbeddingError) as err:
        await e.embed(["refund"])
    assert err.value.kind == "unavailable" and err.value.retryable


def data(*vectors: Any, indexes: list[Any] | None = None) -> dict[str, Any]:
    idx = indexes if indexes is not None else list(range(len(vectors)))
    return {"data": [{"index": i, "embedding": v} for i, v in zip(idx, vectors, strict=True)]}


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (b"not json", "not JSON"),
        ([], "not an object"),
        ({"data": "vectors"}, "data is not a list"),
        (data([1.0, 2.0, 3.0, 4.0]), "one vector for two texts"),
        (
            data(
                [1.0, 2.0, 3.0, 4.0],
                [1.0, 2.0, 3.0],
            ),
            "a vector of the wrong length",
        ),
        (data([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, "3", 4.0]), "a string"),
        (data([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, True, 4.0]), "a boolean"),
        (data([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, float("nan"), 4.0]), "not finite"),
        (data([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0], indexes=[0, 0]), "a repeated index"),
        (data([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0], indexes=[0, 2]), "an index out of range"),
        (data([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0], indexes=[0, None]), "no index"),
        (data([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0], indexes=[False, 1]), "a boolean index"),
    ],
)
async def test_an_answer_of_the_wrong_shape_is_refused(answer: Any, reason: str) -> None:
    fake = FakeProvider()
    raw = answer if isinstance(answer, bytes) else json.dumps(answer, allow_nan=True).encode()
    fake.script = [(200, raw, {"content-type": "application/json"})]
    e, _ = embedder(fake)
    with pytest.raises(EmbeddingError) as err:
        await e.embed(["refund", "order"])
    assert (err.value.kind, err.value.retryable) == ("malformed", False), reason
    assert len(fake.requests) == 1, reason


async def test_an_answer_too_large_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenttwin_core import embeddings

    monkeypatch.setattr(embeddings, "MAX_RESPONSE_BYTES", 200)
    fake = FakeProvider()
    e, _ = embedder(fake)
    assert await e.embed(["short"]) == [vector_of("short")]  # well under the bound
    fake.script = [(200, {"data": [{"index": 0, "embedding": [1.0, 2.0, 3.0, 4.0]}], "pad": "x" * 300}, {})]
    with pytest.raises(EmbeddingError) as err:
        await e.embed(["short"])
    assert err.value.kind == "malformed" and "too large" in str(err.value)


async def test_an_unreadable_retry_after_falls_back_to_backoff() -> None:
    fake = FakeProvider()
    fake.script = [(503, {}, {"retry-after": "nan"}), (503, {}, {"retry-after": "-5"})]
    e, slept = embedder(fake)
    assert await e.embed(["refund"]) == [vector_of("refund")]
    # "nan" is no number of seconds (the backoff of attempt 1); a negative wait is none.
    assert slept == [1.0, 0.0]


async def test_a_real_server_over_a_socket() -> None:
    """Through a socket: the proxy environment is ignored (the key goes to
    the configured endpoint only) and the configured path is used."""
    seen: list[tuple[str, str | None, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, self.headers.get("Authorization"), body))
            out = {"data": [{"index": i, "embedding": vector_of(t)} for i, t in enumerate(body["input"])]}
            raw = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.MonkeyPatch.context() as mp:
            for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                mp.setenv(var, "http://127.0.0.1:9")
            for var in ("NO_PROXY", "no_proxy"):
                mp.delenv(var, raising=False)
            settings = EmbeddingSettings(
                **{**SETTINGS.__dict__, "base_url": f"http://127.0.0.1:{server.server_address[1]}/v1"}
            )
            e = build_embedder(settings)
            assert await e.embed(["refund over the limit"]) == [vector_of("refund over the limit")]
            await e.close()  # type: ignore[attr-defined]
    finally:
        server.shutdown()
        server.server_close()
    [(path, auth, body)] = seen
    assert (path, auth, body["model"]) == (
        "/v1/embeddings",
        "Bearer sk-test-key-0123456789",
        "text-embedding-3-small",
    )


def test_the_hashing_model_is_the_default() -> None:
    loader = Loader({})
    assert load_embedding_settings(loader) == EmbeddingSettings()
    assert loader.errors == []
    assert isinstance(build_embedder(EmbeddingSettings()), HashingEmbedder)
    with pytest.raises(ValueError, match="unknown embedding provider"):
        build_embedder(EmbeddingSettings(provider="word2vec"))


def test_the_hosted_provider_is_configured() -> None:
    loader = Loader(
        {
            "EMBEDDING_PROVIDER": "openai_compatible",
            "EMBEDDING_MODEL": "nomic-embed-text",
            "EMBEDDING_DIMENSIONS": "768",
            "EMBEDDING_BASE_URL": "http://ollama:11434/v1/",
            "EMBEDDING_SEND_DIMENSIONS": "true",
            "EMBEDDING_TIMEOUT": "5s",
            "EMBEDDING_MAX_RETRIES": "1",
            "EMBEDDING_BATCH_SIZE": "16",
        }
    )
    settings = load_embedding_settings(loader)
    assert loader.errors == []
    # A local server needs no key.
    assert settings == EmbeddingSettings(
        provider="openai_compatible",
        model="nomic-embed-text",
        dims=768,
        base_url="http://ollama:11434/v1",
        api_key="",
        send_dimensions=True,
        timeout_s=5.0,
        max_retries=1,
        batch_size=16,
    )
    assert isinstance(build_embedder(settings), OpenAICompatibleEmbedder)


@pytest.mark.parametrize(
    ("env", "problem"),
    [
        ({"EMBEDDING_PROVIDER": "semantic"}, "EMBEDDING_PROVIDER: must be one of hashing|openai_compatible"),
        ({"EMBEDDING_DIMENSIONS": "8", "EMBEDDING_API_KEY": "k"}, "EMBEDDING_MODEL: is required"),
        ({"EMBEDDING_MODEL": "m", "EMBEDDING_API_KEY": "k"}, "EMBEDDING_DIMENSIONS: is required"),
        (
            {"EMBEDDING_MODEL": "m", "EMBEDDING_DIMENSIONS": "16001", "EMBEDDING_API_KEY": "k"},
            "EMBEDDING_DIMENSIONS",
        ),
        (
            {"EMBEDDING_MODEL": "hashing-v2", "EMBEDDING_DIMENSIONS": "8", "EMBEDDING_API_KEY": "k"},
            "not name the local",
        ),
        (
            {"EMBEDDING_MODEL": "m", "EMBEDDING_DIMENSIONS": "8"},
            "EMBEDDING_API_KEY: is required for api.openai.com",
        ),
        (
            {"EMBEDDING_MODEL": "m", "EMBEDDING_DIMENSIONS": "8", "EMBEDDING_BASE_URL": "ftp://tei/v1"},
            "EMBEDDING_BASE_URL: must be an http(s) URL",
        ),
        (
            {"EMBEDDING_MODEL": "m", "EMBEDDING_DIMENSIONS": "8", "EMBEDDING_BASE_URL": "https://u:p@tei/v1"},
            "without credentials",
        ),
    ],
)
def test_a_bad_hosted_configuration_is_refused(env: dict[str, str], problem: str) -> None:
    loader = Loader({"EMBEDDING_PROVIDER": "openai_compatible", **env})
    load_embedding_settings(loader)
    assert any(problem in e for e in loader.errors), loader.errors


def test_the_embedder_refuses_settings_it_cannot_use() -> None:
    with pytest.raises(ValueError, match="EMBEDDING_MODEL"):
        OpenAICompatibleEmbedder(EmbeddingSettings(provider="openai_compatible", dims=8))
    with pytest.raises(ValueError, match="EMBEDDING_DIMENSIONS"):
        OpenAICompatibleEmbedder(EmbeddingSettings(provider="openai_compatible", model="m", dims=0))
