"""Selecting the scenarios a change touches (spec §22): by name, tag,
source and semantic similarity, each scenario with every reason it was
selected for, on a real PostgreSQL with pgvector."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

import httpx
import psycopg
import pytest

from agenttwin_core.auth import Principal, Role
from agenttwin_core.db import Migrator, load_migrations
from agenttwin_core.embeddings import (
    EmbeddingError,
    EmbeddingSettings,
    HashingEmbedder,
    OpenAICompatibleEmbedder,
    vector_literal,
)
from agenttwin_core.ids import new_id
from agenttwin_core.logx import get_logger
from agenttwin_simulation import api as api_module
from agenttwin_simulation.store import SCHEMA
from sim_testutil import AGENT, MIGRATIONS, ORG, OTHER_PROJECT, PROJECT, Stack, simulation_stack, twin_yaml

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

MATCH = "/api/v1/scenarios/match"
VIEWER = Principal(org_id=ORG, actor="user:viewer", role=Role.VIEWER, project_ids=(PROJECT,))
OUTSIDER = Principal(org_id=ORG, actor="user:outsider", role=Role.ENGINEER, project_ids=(OTHER_PROJECT,))

# What the control plane sends for two changes of the demo agent.
PROMPT = (
    "Refunds above 100 USD always go to a human specialist; check the refund policy first. "
    "Mentions refund_payment get_refund_policy escalate_to_human"
)
EXPORT = "tool export_customer_data added: Export all personal data of a customer (administrators only)"
SECURITY = {
    "cross-tenant-order",
    "malicious-retrieved-content",
    "refund-prompt-injection",
    "unauthorized-admin-tool",
}


def scenario(name: str, description: str, *, agent: str | None = AGENT, **meta: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "twin": "demo-co-support",
        "input": {"message": "Hello", "context": {"tenant": "demo-co", "customer_id": "CUS-100"}},
        "expectations": [{"type": "toolCalled", "tool": "lookup_order"}],
    }
    if agent:
        spec["agent"] = agent
    return {
        "apiVersion": "agenttwin.dev/v1",
        "kind": "Scenario",
        "metadata": {"name": name, "severity": "medium", "description": description, **meta},
        "spec": spec,
    }


async def save(s: Stack, doc: dict[str, Any]) -> str:
    r = await s.call("POST", "/api/v1/scenarios", {"project_id": PROJECT, "document": doc})
    assert r.status_code in (200, 201), r.text
    return str(r.json()["scenario"]["id"])


async def match(s: Stack, **body: Any) -> dict[str, Any]:
    out: dict[str, Any] = await s.ok("POST", MATCH, {"project_id": PROJECT} | body)
    return out


def by_name(out: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {m["name"]: m for m in out["scenarios"]}


def similar_to(out: dict[str, Any], query: str) -> list[str]:
    """The scenarios a query selected, closest first."""
    hits = [
        (x["similarity"], m["name"])
        for m in out["scenarios"]
        for x in m["matched"]["similar"]
        if x["query"] == query
    ]
    return [name for _, name in sorted(hits, key=lambda h: (-h[0], h[1]))]


async def test_a_change_selects_scenarios_and_says_why() -> None:
    async with simulation_stack() as s:
        suite = await s.register_suite()
        out = await match(
            s,
            agent=AGENT,
            names=["refund-happy-path", "no-such-scenario"],
            tags=["security"],
            queries=[
                {"id": "prompt:system", "text": PROMPT},
                {"id": "tool:export_customer_data", "text": EXPORT},
                {"id": "empty", "text": "the and of it"},
            ],
        )
        got = by_name(out)
        assert out["embedding_model"] == "hashing-v1" and out["truncated"] is False
        assert out["unknown_names"] == ["no-such-scenario"] and out["unembedded_queries"] == ["empty"]

        # Named (the dependency graph's links): selected for that reason.
        assert got["refund-happy-path"]["matched"]["name"] is True
        assert [n for n, m in got.items() if m["matched"]["name"]] == ["refund-happy-path"]
        # The always-run tag: every scenario that carries it, and no other.
        assert {n for n, m in got.items() if m["matched"]["tags"] == ["security"]} == SECURITY
        # Semantic: the refund limit change is closest to the refund limit
        # scenario and selects refund scenarios, not the data export one.
        prompt = similar_to(out, "prompt:system")
        assert prompt[0] == "refund-over-limit"
        assert {"refund-prompt-injection", "refund-happy-path"} <= set(prompt)
        assert not {"unauthorized-admin-tool", "cross-tenant-order"} & set(prompt)
        # A new tool is close to the scenario about it, and to nothing else.
        assert similar_to(out, "tool:export_customer_data") == ["unauthorized-admin-tool"]

        # Every scenario is there for a reason; nothing else is there.
        for m in out["scenarios"]:
            r = m["matched"]
            assert r["name"] or r["tags"] or r["source"] or r["similar"], m["name"]
            assert all(0.25 <= x["similarity"] <= 1 for x in r["similar"])
        assert set(got) <= set(suite)
        # Severest first, then by name.
        order = [
            (("critical", "high", "medium", "low").index(m["severity"]), m["name"]) for m in out["scenarios"]
        ]
        assert order == sorted(order)
        # A query is named by its id; its text is never repeated.
        answer = json.dumps(out)
        assert all(q[:30] not in answer and q[-30:] not in answer for q in (PROMPT, EXPORT))

        # A vector per active scenario, of the model in use.
        rows = await s.store.all(
            "SELECT model, recipe, dims, embedding IS NOT NULL AS has FROM scenario_embedding"
        )
        assert len(rows) == len(suite)
        assert {(r["model"], r["recipe"], r["dims"], r["has"]) for r in rows} == {
            ("hashing-v1", "scenario-text-v1", 256, True)
        }


async def test_thresholds_and_limits_of_similarity() -> None:
    async with simulation_stack() as s:
        await s.register_suite()
        q = [{"id": "p", "text": PROMPT}]
        wide = similar_to(await match(s, queries=q, min_similarity=0.0, max_per_query=50), "p")
        assert len(wide) == 9
        assert similar_to(await match(s, queries=q, min_similarity=0.0, max_per_query=2), "p") == wide[:2]
        assert similar_to(await match(s, queries=q, min_similarity=0.99), "p") == []
        # Unrelated text reaches no scenario at the default threshold.
        assert (await match(s, queries=[{"id": "g", "text": "gift card balance lookup"}]))["scenarios"] == []
        # Nothing asked, nothing selected.
        empty = await match(s)
        assert (empty["scenarios"], empty["unknown_names"], empty["truncated"]) == ([], [], False)


async def test_known_regressions_are_selected_by_source() -> None:
    async with simulation_stack() as s:
        await s.register_demo("refund-happy-path")
        await save(
            s, scenario("regressed-refund-twice", "Mined from production.", source="production_regression")
        )
        out = await match(s, sources=["production_regression"])
        assert [(m["name"], m["source"], m["matched"]["source"]) for m in out["scenarios"]] == [
            ("regressed-refund-twice", "production_regression", True)
        ]


async def test_similarity_follows_the_latest_version_and_archiving() -> None:
    async with simulation_stack() as s:
        await s.ok("POST", "/api/v1/twins", {"project_id": PROJECT, "yaml": twin_yaml()}, status=201)
        gift = [{"id": "q", "text": "gift card balance"}]
        ship = [{"id": "q", "text": "change the shipping address of an order"}]
        sid = await save(s, scenario("balance-question", "The customer asks for the balance of a gift card."))
        retired = await save(
            s, scenario("retired-balance", "The customer asks for the balance of a gift card.")
        )
        await s.ok("POST", f"/api/v1/scenarios/{retired}/archive")
        assert [m["id"] for m in (await match(s, queries=gift))["scenarios"]] == [sid]
        # An archived scenario is not embedded at all.
        embedded = await s.store.all("SELECT scenario_id FROM scenario_embedding")
        assert [str(r["scenario_id"]) for r in embedded] == [sid]

        # A new version is embedded again when it is next matched.
        await save(s, scenario("balance-question", "The customer wants to change the shipping address."))
        assert (await match(s, queries=gift))["scenarios"] == []
        assert [m["id"] for m in (await match(s, queries=ship))["scenarios"]] == [sid]
        row = await s.store.one("SELECT version FROM scenario_embedding WHERE scenario_id = %s", (sid,))
        assert row == {"version": 2}

        # An archived scenario is selected for nothing, not even by name.
        await s.ok("POST", f"/api/v1/scenarios/{sid}/archive")
        out = await match(s, names=["balance-question"], queries=ship)
        assert (out["scenarios"], out["unknown_names"]) == ([], ["balance-question"])


async def test_a_vector_of_another_model_or_recipe_is_computed_again() -> None:
    async with simulation_stack() as s:
        await s.register_demo("refund-happy-path", "refund-over-limit")
        q = [{"id": "p", "text": PROMPT}]
        before = similar_to(await match(s, queries=q), "p")
        # Stale rows: another model, another recipe, a wrong vector. They are
        # never compared with the current model; they are replaced.
        await s.store.all("UPDATE scenario_embedding SET model = 'other-model', embedding = NULL RETURNING 1")
        assert similar_to(await match(s, queries=q), "p") == before
        await s.store.all("UPDATE scenario_embedding SET recipe = 'old-recipe', embedding = NULL RETURNING 1")
        assert similar_to(await match(s, queries=q), "p") == before
        rows = await s.store.all("SELECT DISTINCT model, recipe FROM scenario_embedding")
        assert rows == [{"model": "hashing-v1", "recipe": "scenario-text-v1"}]


async def test_scenarios_of_other_agents_are_left_out() -> None:
    async with simulation_stack() as s:
        await s.ok("POST", "/api/v1/twins", {"project_id": PROJECT, "yaml": twin_yaml()}, status=201)
        text = "The customer asks for a refund of a delivered order."
        await save(s, scenario("mine", text))
        await save(s, scenario("theirs", text, agent="billing-agent"))
        await save(s, scenario("everyones", text, agent=None))
        q = [{"id": "q", "text": "refund a delivered order"}]
        assert set(by_name(await match(s, agent=AGENT, queries=q))) == {"mine", "everyones"}
        out = await match(s, agent=AGENT, names=["theirs"])
        assert (out["scenarios"], out["unknown_names"]) == ([], ["theirs"])
        # Without an agent, every agent's scenarios.
        assert set(by_name(await match(s, queries=q))) == {"mine", "theirs", "everyones"}


async def test_a_wide_tag_never_pushes_out_a_named_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    async with simulation_stack() as s:
        await s.register_suite()
        monkeypatch.setattr(api_module, "MAX_MATCHED", 3)
        out = await match(s, names=["unauthorized-admin-tool", "refund-rate-limited"], tags=["refunds"])
        assert out["truncated"] is True and len(out["scenarios"]) == 3
        named = {m["name"] for m in out["scenarios"] if m["matched"]["name"]}
        assert named == {"unauthorized-admin-tool", "refund-rate-limited"}
        assert out["unknown_names"] == []


async def test_concurrent_matches_embed_each_scenario_once() -> None:
    async with simulation_stack() as s:
        suite = await s.register_suite()
        q = [{"id": "p", "text": PROMPT}]
        outs = await asyncio.gather(*(match(s, queries=q) for _ in range(6)))
        assert all(o == outs[0] for o in outs)
        rows = await s.store.all("SELECT count(*) AS n FROM scenario_embedding")
        assert rows == [{"n": len(suite)}]


async def test_access_and_validation() -> None:
    async with simulation_stack() as s:
        await s.register_demo("refund-happy-path")
        # Reading is enough.
        viewer = await s.call(
            "POST", MATCH, {"project_id": PROJECT, "names": ["refund-happy-path"]}, as_=VIEWER
        )
        assert viewer.status_code == 200 and len(viewer.json()["scenarios"]) == 1
        # A project outside the caller's access is not found.
        for as_, project in ((OUTSIDER, PROJECT), (None, OTHER_PROJECT)):
            r = await s.call("POST", MATCH, {"project_id": project}, as_=as_)
            assert (r.status_code, r.json()["error"]["code"]) == (404, "NOT_FOUND")
        refusals = [
            ({"project_id": "nope"}, "INVALID_PARAMETER"),
            ({"project_id": PROJECT, "extra": 1}, "INVALID_REQUEST"),
            ({"project_id": PROJECT, "names": ["Not A Name"]}, "INVALID_REQUEST"),
            ({"project_id": PROJECT, "tags": ["x" * 64]}, "INVALID_REQUEST"),
            ({"project_id": PROJECT, "sources": ["folklore"]}, "INVALID_REQUEST"),
            ({"project_id": PROJECT, "min_similarity": 1.5}, "INVALID_REQUEST"),
            ({"project_id": PROJECT, "max_per_query": 0}, "INVALID_REQUEST"),
            ({"project_id": PROJECT, "queries": [{"id": "a", "text": ""}]}, "INVALID_REQUEST"),
            ({"project_id": PROJECT, "queries": [{"id": "has space", "text": "x"}]}, "INVALID_REQUEST"),
            ({"project_id": PROJECT, "queries": [{"id": "a", "text": "x" * 8001}]}, "INVALID_REQUEST"),
            ({"project_id": PROJECT, "queries": [{"id": "a", "text": "x"}] * 51}, "INVALID_REQUEST"),
            ({"project_id": PROJECT, "names": ["a"] * 501}, "INVALID_REQUEST"),
            (
                {"project_id": PROJECT, "queries": [{"id": "a", "text": "x"}, {"id": "a", "text": "y"}]},
                "INVALID_REQUEST",
            ),
        ]
        for body, code in refusals:
            r = await s.call("POST", MATCH, body)
            assert (r.status_code, r.json()["error"]["code"]) == (400, code), body


async def test_the_schema_keeps_an_embedding_in_its_scenarios_tenant() -> None:
    async with simulation_stack() as s:
        await s.register_demo("refund-happy-path")
        await match(s, queries=[{"id": "p", "text": PROMPT}])
        row = await s.store.one("SELECT scenario_id FROM scenario_embedding")
        assert row is not None
        for column, value in (("project_id", OTHER_PROJECT), ("organization_id", new_id())):
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                await s.store.all(
                    f"UPDATE scenario_embedding SET {column} = %s WHERE scenario_id = %s RETURNING 1",  # noqa: S608
                    (value, row["scenario_id"]),
                )
        # The vector's length is the one its row declares.
        with pytest.raises(psycopg.errors.CheckViolation):
            await s.store.all("UPDATE scenario_embedding SET embedding = '[1,2,3]' RETURNING 1")


async def test_the_migration_rolls_back_and_applies_again() -> None:
    async with simulation_stack() as s:
        await s.register_demo("refund-happy-path")
        q = [{"id": "p", "text": PROMPT}]
        before = await match(s, queries=q)
        migrator = Migrator(s.pool, SCHEMA, load_migrations(MIGRATIONS))
        assert await migrator.down(1) == 1
        gone = await s.store.one(
            """SELECT to_regclass('scenario_embedding') AS tbl,
                      (SELECT count(*) FROM pg_constraint WHERE conname = 'scenario_tenancy') AS cons,
                      (SELECT count(*) FROM pg_extension WHERE extname = 'vector') AS ext"""
        )
        # The extension stays: other schemas may use it.
        assert gone == {"tbl": None, "cons": 0, "ext": 1}
        assert await migrator.up() == 1
        assert await match(s, queries=q) == before


async def test_only_current_vectors_of_the_project_are_compared() -> None:
    async with simulation_stack() as s:
        await s.register_demo("refund-happy-path", "refund-over-limit")
        # The stored vectors are the current ones after a match.
        await match(s, queries=[{"id": "p", "text": PROMPT}])
        vec = vector_literal(HashingEmbedder().embed_one(PROMPT))
        args: dict[str, Any] = {
            "org": ORG,
            "project": PROJECT,
            "agent": AGENT,
            "model": "hashing-v1",
            "dims": 256,
            "recipe": "scenario-text-v1",
            "queries": [("p", vec)],
            "per_query": 10,
            "min_similarity": 0.0,
        }
        assert len(await s.store.similar_scenarios(**args)) == 2
        # Another tenant, model, space, recipe or agent: nothing to compare.
        for change in (
            {"org": new_id()},
            {"project": OTHER_PROJECT},
            {"model": "other-model"},
            {"dims": 64, "queries": [("p", vector_literal(HashingEmbedder(64).embed_one(PROMPT)))]},
            {"recipe": "old-recipe"},
            {"agent": "billing-agent"},
        ):
            assert await s.store.similar_scenarios(**(args | change)) == [], change
        # A vector of an older version, or of an archived scenario, is not compared.
        await s.store.all(
            "UPDATE scenario_embedding e SET version = 7 FROM scenario s "
            "WHERE s.id = e.scenario_id AND s.name = 'refund-happy-path' RETURNING 1"
        )
        await s.store.all("UPDATE scenario SET archived = true WHERE name = 'refund-over-limit' RETURNING 1")
        assert await s.store.similar_scenarios(**args) == []


class WrongShape(HashingEmbedder):
    model = "wrong-shape"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0] * 8 for _ in texts]


async def test_a_provider_answering_the_wrong_shape_is_an_error() -> None:
    async with simulation_stack(embedder=WrongShape()) as s:
        await s.register_demo("refund-happy-path")
        r = await s.call("POST", MATCH, {"project_id": PROJECT, "queries": [{"id": "p", "text": PROMPT}]})
        assert (r.status_code, r.json()["error"]["code"]) == (500, "INTERNAL")
        # Nothing of the wrong shape was stored.
        assert await s.store.all("SELECT 1 FROM scenario_embedding") == []


class Down(HashingEmbedder):
    model = "hosted-model"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise EmbeddingError("rate_limited", "The embedding provider answered HTTP 429.", retryable=True)


async def test_a_provider_that_cannot_answer_is_unavailable_not_an_empty_selection() -> None:
    async with simulation_stack(embedder=Down()) as s:
        await s.register_demo("refund-happy-path")
        r = await s.call("POST", MATCH, {"project_id": PROJECT, "queries": [{"id": "p", "text": PROMPT}]})
        # A 503 the caller can retry: "nothing is similar" would be a lie.
        assert r.status_code == 503, r.text
        error = r.json()["error"]
        assert error["code"] == "EMBEDDINGS_UNAVAILABLE"
        assert "hosted-model" in error["message"] and "429" in error["message"]
        assert await s.store.all("SELECT 1 FROM scenario_embedding") == []


async def test_a_hosted_provider_selected_by_configuration() -> None:
    """The OpenAI-compatible adapter end to end, against a fake endpoint that
    embeds like hashing-v1 under another model name: the vectors are stored
    and compared under that model, and the answer names it."""
    local = HashingEmbedder()
    sent: list[list[str]] = []

    async def endpoint(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body["input"])
        vectors = await local.embed(body["input"])
        return httpx.Response(
            200, json={"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]}
        )

    settings = EmbeddingSettings(
        provider="openai_compatible",
        model="fake-semantic-1",
        dims=local.dims,
        base_url="http://embeddings.test/v1",
    )
    hosted = OpenAICompatibleEmbedder(settings, transport=httpx.MockTransport(endpoint))
    names = ("refund-happy-path", "refund-over-limit", "cross-tenant-order")
    async with simulation_stack(embedder=hosted) as s:
        await s.register_demo(*names)
        out = await match(s, queries=[{"id": "prompt", "text": PROMPT}])
        assert out["embedding_model"] == "fake-semantic-1"
        assert similar_to(out, "prompt")[0] == "refund-over-limit"
        stored = await s.store.all("SELECT DISTINCT model, dims FROM scenario_embedding")
        assert stored == [{"model": "fake-semantic-1", "dims": local.dims}]
        # Each scenario and the query were sent once; matching again sends
        # only the query (the stored vectors are of this model).
        texts = [t for batch in sent for t in batch]
        assert len(texts) == len(names) + 1 and PROMPT in texts
        await match(s, queries=[{"id": "prompt", "text": PROMPT}])
        assert [t for batch in sent for t in batch][len(texts) :] == [PROMPT]
    await hosted.close()


async def test_a_scenario_with_nothing_to_compare_is_never_similar(monkeypatch: pytest.MonkeyPatch) -> None:
    async with simulation_stack() as s:
        await s.register_demo("refund-happy-path")
        monkeypatch.setattr(api_module, "scenario_text", lambda doc: "the and of it")
        out = await match(s, queries=[{"id": "p", "text": PROMPT}], min_similarity=0.0)
        # Its vector is not stored (a zero vector has no direction: its
        # cosine is undefined, and PostgreSQL would order NaN first).
        assert out["scenarios"] == []
        assert await s.store.all("SELECT embedding IS NULL AS empty FROM scenario_embedding") == [
            {"empty": True}
        ]


async def test_the_providers_vectors_are_checked() -> None:
    api = api_module.SimulationAPI(
        store=None,  # type: ignore[arg-type]
        cfg=None,  # type: ignore[arg-type]
        registry=None,  # type: ignore[arg-type]
        control_plane=None,  # type: ignore[arg-type]
        log=get_logger("test"),
        embedder=WrongShape(),
    )
    with pytest.raises(RuntimeError, match="wrong-shape answered vectors of the wrong shape"):
        await api._embed(["one"])

    class TooFew(HashingEmbedder):
        async def embed(self, texts: Sequence[str]) -> list[list[float]]:
            return [self.embed_one(t) for t in texts][:-1]

    api.embedder = TooFew()
    with pytest.raises(RuntimeError, match="wrong shape"):
        await api._embed(["one", "two"])
    api.embedder = HashingEmbedder()
    assert len((await api._embed(["one", "two"]))[1]) == 256
