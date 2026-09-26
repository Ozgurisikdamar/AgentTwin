"""Text embeddings behind a provider interface (spec §22 "Semantic scenario
matching", §49 "Embeddings", §50).

``HashingEmbedder`` is the local default: deterministic, dependency-free and
fast. It is *lexical*: a text becomes a signed feature-hashed bag of words
and word pairs, so two texts are similar when they share (stemmed) words and
identifiers — "refunds above the limit" and "refund_payment over the refund
limit" are, "reimbursement" and "refund" are not.

``OpenAICompatibleEmbedder`` gives semantic similarity from any
``/embeddings`` endpoint of the OpenAI API's shape (OpenAI, Hugging Face TEI,
Ollama, vLLM): it is selected by configuration (``EMBEDDING_PROVIDER``,
:func:`load_embedding_settings`). Every vector is stored with the ``model``
that produced it and only vectors of the same model are ever compared.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

import httpx

from agenttwin_core.config import Loader
from agenttwin_core.retry import retry_after_seconds

__all__ = [
    "HASHING_DIMS",
    "HASHING_MODEL",
    "MAX_TEXT_CHARS",
    "EmbeddingError",
    "EmbeddingProvider",
    "EmbeddingSettings",
    "HashingEmbedder",
    "OpenAICompatibleEmbedder",
    "build_embedder",
    "cosine",
    "features",
    "is_zero",
    "load_embedding_settings",
    "vector_literal",
]

HASHING_MODEL = "hashing-v1"
HASHING_DIMS = 256
# Longer text is cut: what an embedding describes is its beginning.
MAX_TEXT_CHARS = 20_000


class EmbeddingProvider(Protocol):
    """Turns texts into vectors of one space.

    ``model`` names the space (a vector of another model is never compared
    with one of this); ``dims`` is the length of every vector. A text without
    anything to embed yields the zero vector."""

    model: str
    dims: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


# Words that carry no meaning of their own in the texts compared here.
# fmt: off
_STOPWORDS = frozenset((
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any", "are", "as", "at",
    "be", "because", "been", "before", "being", "below", "between", "both", "but", "by", "can", "could",
    "did", "do", "does", "doing", "down", "during", "each", "few", "for", "from", "further", "had", "has",
    "have", "having", "he", "her", "here", "hers", "him", "his", "how", "i", "if", "in", "into", "is",
    "it", "its", "itself", "just", "me", "more", "most", "my", "no", "nor", "not", "now", "of", "off",
    "on", "once", "only", "or", "other", "our", "ours", "out", "over", "own", "same", "she", "should",
    "so", "some", "such", "than", "that", "the", "their", "theirs", "them", "then", "there", "these",
    "they", "this", "those", "through", "to", "too", "under", "until", "up", "very", "was", "we", "were",
    "what", "when", "where", "which", "while", "who", "whom", "why", "will", "with", "would", "you",
    "your", "yours", "must", "may", "might", "shall", "also", "via", "per",
))
# fmt: on

# Words (letters and digits), joined into one identifier by "_", "-" or ".".
_WORD = re.compile(r"[^\W_]+(?:[_.\-][^\W_]+)*")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_JOINERS = re.compile(r"[_.\-]")

# Weight of each kind of feature: a pair of words says less than a word.
_WEIGHT = {"w": 1.0, "c": 1.0, "b": 0.5}


def _stem(word: str) -> str:
    """A light suffix stripper: "refunds", "refunded" and "refunding" are
    "refund"; "policies" is "policy". Both sides of a comparison are stemmed
    the same way, so the stems only need to agree, not to be words."""
    if len(word) > 5 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif len(word) > 5 and word.endswith("ing"):
        word = word[:-3]
    elif len(word) > 4 and word.endswith(("ed", "sses", "xes", "zes", "ches", "shes")):
        word = word[:-2]
    elif len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    if len(word) > 4 and word.endswith("e"):
        word = word[:-1]
    return word


def _keep(word: str) -> bool:
    return len(word) > 1 and word not in _STOPWORDS


def features(text: str) -> Counter[str]:
    """The weighted features of a text: stemmed words (``w:``), joined
    identifiers as a whole (``c:``, "refund_payment" besides "refund" and
    "payment") and pairs of neighbouring words (``b:``)."""
    text = unicodedata.normalize("NFKC", text[:MAX_TEXT_CHARS])
    text = _CAMEL.sub("_", text).casefold()
    out: Counter[str] = Counter()
    words: list[str] = []
    for m in _WORD.finditer(text):
        parts = [_stem(p) for p in _JOINERS.split(m.group()) if p]
        kept = [p for p in parts if _keep(p)]
        if len(parts) > 1 and kept:
            out["c:" + "_".join(parts)] += 1
        for p in kept:
            out["w:" + p] += 1
        words.extend(kept)
    for a, b in itertools.pairwise(words):
        out["b:" + a + " " + b] += 1
    return out


def _slot(feature: str, dims: int) -> tuple[int, float]:
    h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
    return h % dims, 1.0 if (h >> 63) & 1 else -1.0


class HashingEmbedder:
    """Signed feature hashing of :func:`features` with sublinear term
    frequency, L2-normalized. Deterministic across processes and versions of
    Python (no ``hash()``)."""

    model = HASHING_MODEL

    def __init__(self, dims: int = HASHING_DIMS) -> None:
        if dims < 16:
            raise ValueError("dims must be at least 16")
        self.dims = dims

    def embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dims
        for feature, count in features(text).items():
            index, sign = _slot(feature, self.dims)
            vec[index] += sign * _WEIGHT[feature[0]] * (1.0 + math.log(count))
        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec] if norm > 0 else vec

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]


# ------------------------------------------------------ hosted providers

OPENAI_BASE_URL = "https://api.openai.com/v1"
# pgvector's limit for the vector type.
MAX_DIMS = 16_000
MAX_RETRY_AFTER_S = 10.0
# A batch of the largest vectors fits comfortably; a larger answer is refused.
MAX_RESPONSE_BYTES = 64 << 20
_RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504, 529})

EmbeddingErrorKind = Literal["unavailable", "rate_limited", "timeout", "rejected", "malformed"]


class EmbeddingError(Exception):
    """A provider that could not embed. ``retryable`` errors are worth
    another attempt later (the provider is down, busy or slow)."""

    def __init__(self, kind: EmbeddingErrorKind, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


@dataclass(frozen=True)
class EmbeddingSettings:
    """What ``EMBEDDING_*`` configures (:func:`load_embedding_settings`)."""

    provider: str = "hashing"  # hashing | openai_compatible
    model: str = ""
    dims: int = HASHING_DIMS
    base_url: str = OPENAI_BASE_URL
    api_key: str = field(default="", repr=False)
    # Send ``dimensions`` (OpenAI's text-embedding-3 models shorten their
    # vectors to it); servers that do not know the field may reject it.
    send_dimensions: bool = False
    timeout_s: float = 30.0
    max_retries: int = 2
    batch_size: int = 32


def load_embedding_settings(loader: Loader) -> EmbeddingSettings:
    """``EMBEDDING_PROVIDER`` (``hashing`` | ``openai_compatible``) and, for
    a hosted provider, ``EMBEDDING_MODEL``, ``EMBEDDING_DIMENSIONS`` (the
    length of its vectors), ``EMBEDDING_BASE_URL``, ``EMBEDDING_API_KEY``
    (required by api.openai.com; local servers may not need one),
    ``EMBEDDING_SEND_DIMENSIONS``, ``EMBEDDING_TIMEOUT``,
    ``EMBEDDING_MAX_RETRIES`` and ``EMBEDDING_BATCH_SIZE``."""
    provider = loader.one_of("EMBEDDING_PROVIDER", "hashing", "hashing", "openai_compatible")
    if provider == "hashing":
        return EmbeddingSettings()
    model = loader.string("EMBEDDING_MODEL")
    loader.check(bool(model), "EMBEDDING_MODEL", "is required for the openai_compatible provider")
    loader.check(
        len(model) <= 200 and not model.lower().startswith("hashing"),
        "EMBEDDING_MODEL",
        "must be at most 200 characters and not name the local hashing model",
    )
    dims = loader.integer("EMBEDDING_DIMENSIONS", 0, 1, MAX_DIMS)
    loader.check(dims > 0, "EMBEDDING_DIMENSIONS", "is required for the openai_compatible provider")
    base_url = loader.string("EMBEDDING_BASE_URL", OPENAI_BASE_URL).rstrip("/")
    url = urlparse(base_url)
    loader.check(
        url.scheme in ("http", "https") and bool(url.hostname) and not url.username and not url.password,
        "EMBEDDING_BASE_URL",
        "must be an http(s) URL without credentials",
    )
    api_key = loader.string("EMBEDDING_API_KEY")
    loader.check(
        bool(api_key) or url.hostname != urlparse(OPENAI_BASE_URL).hostname,
        "EMBEDDING_API_KEY",
        "is required for api.openai.com",
    )
    return EmbeddingSettings(
        provider=provider,
        model=model,
        dims=dims,
        base_url=base_url,
        api_key=api_key,
        send_dimensions=loader.boolean("EMBEDDING_SEND_DIMENSIONS", False),
        timeout_s=loader.duration("EMBEDDING_TIMEOUT", 30.0),
        max_retries=loader.integer("EMBEDDING_MAX_RETRIES", 2, 0, 10),
        batch_size=loader.integer("EMBEDDING_BATCH_SIZE", 32, 1, 2048),
    )


class OpenAICompatibleEmbedder:
    """``POST {base_url}/embeddings`` of the OpenAI API's shape, in batches.

    Blank texts are not sent (their vector is zero, as with the hashing
    model); longer texts are cut to ``MAX_TEXT_CHARS``. Every answer is
    checked: one vector per text, matched by ``index``, each of ``dims``
    finite numbers. Busy or failing providers are retried a few times with
    backoff (``Retry-After`` honored up to 10 s); a refusal is not."""

    def __init__(
        self,
        settings: EmbeddingSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not settings.model:
            raise ValueError("EMBEDDING_MODEL is required for the openai_compatible provider")
        if not 1 <= settings.dims <= MAX_DIMS:
            raise ValueError(f"EMBEDDING_DIMENSIONS must be between 1 and {MAX_DIMS}")
        self.settings = settings
        self.model = settings.model
        self.dims = settings.dims
        self._url = settings.base_url.rstrip("/") + "/embeddings"
        self._headers = {"content-type": "application/json", "accept": "application/json"}
        if settings.api_key:
            self._headers["authorization"] = f"Bearer {settings.api_key}"
        self._sleep = sleep
        # No proxies from the environment, no redirects: the key goes to the
        # configured endpoint only.
        self._client = httpx.AsyncClient(
            timeout=settings.timeout_s, trust_env=False, follow_redirects=False, transport=transport
        )

    def __repr__(self) -> str:  # never print the key
        return f"OpenAICompatibleEmbedder(model={self.model!r}, dims={self.dims}, url={self._url!r})"

    async def close(self) -> None:
        await self._client.aclose()

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = [[0.0] * self.dims for _ in texts]
        todo = [(i, t[:MAX_TEXT_CHARS]) for i, t in enumerate(texts) if t.strip()]
        size = self.settings.batch_size
        for start in range(0, len(todo), size):
            batch = todo[start : start + size]
            vectors = await self._embed_batch([t for _, t in batch])
            for (i, _), vec in zip(batch, vectors, strict=True):
                out[i] = vec
        return out

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload: dict[str, Any] = {"model": self.model, "input": texts, "encoding_format": "float"}
        if self.settings.send_dimensions:
            payload["dimensions"] = self.dims
        attempts = self.settings.max_retries + 1
        for attempt in range(1, attempts + 1):
            wait = min(2.0 ** (attempt - 1), MAX_RETRY_AFTER_S)
            try:
                resp = await self._client.post(self._url, headers=self._headers, json=payload)
            except httpx.TimeoutException:
                error = EmbeddingError(
                    "timeout", "The embedding provider did not answer in time.", retryable=True
                )
            except httpx.HTTPError as err:
                error = EmbeddingError(
                    "unavailable",
                    f"The embedding provider is unreachable ({type(err).__name__}).",
                    retryable=True,
                )
            else:
                if resp.status_code == 200:
                    return self._vectors(resp, len(texts))
                if resp.status_code not in _RETRY_STATUSES:
                    raise EmbeddingError(
                        "rejected", f"The embedding provider refused the request (HTTP {resp.status_code})."
                    )
                kind: EmbeddingErrorKind = "rate_limited" if resp.status_code == 429 else "unavailable"
                error = EmbeddingError(
                    kind, f"The embedding provider answered HTTP {resp.status_code}.", retryable=True
                )
                wait = _retry_after(resp, attempt)
            if attempt == attempts:
                raise error
            await self._sleep(wait)
        raise AssertionError("unreachable")  # pragma: no cover

    def _vectors(self, resp: httpx.Response, count: int) -> list[list[float]]:
        if len(resp.content) > MAX_RESPONSE_BYTES:
            raise EmbeddingError("malformed", "The embedding provider's answer is too large.")
        try:
            body = resp.json()
        except ValueError:
            raise EmbeddingError("malformed", "The embedding provider's answer is not JSON.") from None
        data = body.get("data") if isinstance(body, Mapping) else None
        if not isinstance(data, list) or len(data) != count:
            raise EmbeddingError("malformed", f"The embedding provider did not answer {count} vector(s).")
        out: list[list[float] | None] = [None] * count
        for item in data:
            index = item.get("index") if isinstance(item, Mapping) else None
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or not 0 <= index < count
                or out[index] is not None
            ):
                raise EmbeddingError(
                    "malformed", "The embedding provider's answer has a bad or repeated index."
                )
            out[index] = _vector(item.get("embedding"), self.dims)
        return [v for v in out if v is not None]


def _vector(raw: Any, dims: int) -> list[float]:
    if not isinstance(raw, list) or len(raw) != dims:
        raise EmbeddingError(
            "malformed", f"The embedding provider answered a vector that is not {dims} numbers."
        )
    vec: list[float] = []
    for x in raw:
        if isinstance(x, bool) or not isinstance(x, int | float) or not math.isfinite(x):
            raise EmbeddingError("malformed", "The embedding provider answered a vector with a non-number.")
        vec.append(float(x))
    return vec


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    return retry_after_seconds(resp.headers.get("retry-after"), 2.0 ** (attempt - 1), MAX_RETRY_AFTER_S)


def build_embedder(
    settings: EmbeddingSettings, *, transport: httpx.AsyncBaseTransport | None = None
) -> EmbeddingProvider:
    """The provider the configuration selects (``EMBEDDING_PROVIDER``)."""
    if settings.provider == "hashing":
        return HashingEmbedder()
    if settings.provider == "openai_compatible":
        return OpenAICompatibleEmbedder(settings, transport=transport)
    raise ValueError(f"unknown embedding provider {settings.provider!r} (hashing or openai_compatible)")


def is_zero(vec: Sequence[float]) -> bool:
    return not any(vec)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """The cosine similarity of two vectors of one model (0 when either is zero)."""
    if len(a) != len(b):
        raise ValueError("vectors of different dimensions")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def vector_literal(vec: Sequence[float]) -> str:
    """The pgvector text form of a vector (``'[0.1,-0.2,…]'``)."""
    return "[" + ",".join(format(x, ".7g") for x in vec) + "]"
