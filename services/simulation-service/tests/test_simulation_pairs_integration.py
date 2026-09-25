"""The baseline and candidate runs of an evaluation (spec §27, ADR-0023), on a
real PostgreSQL: created together over one pinned suite, so the agent version
is the only difference between them; one pair per evaluation run, whoever
asks and however often."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from agenttwin_core.auth import Principal, Role, service_principal
from sim_testutil import AGENT, ORG, OTHER_PROJECT, PROJECT, Stack, simulation_stack

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PAIRS = "/internal/v1/simulation-pairs"
EVALUATION = service_principal("evaluation-service", ORG, PROJECT)
CHOSEN = ["refund-happy-path", "refund-timeout-after-mutation"]
# A run orders its cases by severity, then name: the critical scenario first.
IN_RUN_ORDER = ["refund-timeout-after-mutation", "refund-happy-path"]


def pair_body(eval_run_id: str, **over: Any) -> dict[str, Any]:
    return {
        "project_id": PROJECT,
        "eval_run_id": eval_run_id,
        "agent": AGENT,
        "baseline_version": "1.2.4",
        "candidate_version": "1.3.0",
        "scenarios": CHOSEN,
        "seed": 42,
        "requested_by": "user:engineer",
    } | over


async def ask(s: Stack, body: dict[str, Any], as_: Principal = EVALUATION) -> Any:
    return await s.call("POST", PAIRS, body, as_=as_)


async def runs_of(s: Stack, eval_run_id: str) -> list[dict[str, Any]]:
    return await s.store.all(
        "SELECT id, side, status FROM simulation_run WHERE eval_run_id = %s ORDER BY side", (eval_run_id,)
    )


async def test_a_pair_runs_both_versions_over_one_pinned_suite() -> None:
    async with simulation_stack() as s:
        await s.register_suite()
        eval_run = str(uuid.uuid4())
        resp = await ask(s, pair_body(eval_run))
        assert resp.status_code == 201, resp.text
        pair = resp.json()
        base, cand = pair["baseline"], pair["candidate"]
        assert (pair["eval_run_id"], pair["created"], pair["seed"]) == (eval_run, True, 42)
        assert (base["side"], cand["side"]) == ("BASELINE", "CANDIDATE")
        assert (base["agent_version"], cand["agent_version"]) == ("1.2.4", "1.3.0")
        for run in (base, cand):
            assert (run["eval_run_id"], run["status"], run["requested_by"]) == (
                eval_run,
                "QUEUED",
                "user:engineer",
            )
        # One suite: the same scenario versions, twin definitions and seeds on both sides.
        assert base["pinning"]["scenarios"] == cand["pinning"]["scenarios"]
        assert [p["scenario"] for p in base["pinning"]["scenarios"]] == IN_RUN_ORDER
        assert base["pinning"]["seed"] == cand["pinning"]["seed"] == 42
        assert base["pinning"]["agent"]["manifest_sha256"] != cand["pinning"]["agent"]["manifest_sha256"]
        assert base["pinning"]["pair"] == {
            "eval_run_id": eval_run,
            "side": "BASELINE",
            "counterpart_run_id": cand["id"],
        }
        assert cand["pinning"]["pair"]["counterpart_run_id"] == base["id"]
        pinned = {p["scenario"]: p["seed"] for p in base["pinning"]["scenarios"]}
        assert [(c["position"], c["scenario_name"], c["seed"]) for c in pair["cases"]] == [
            (i, name, pinned[name]) for i, name in enumerate(IN_RUN_ORDER)
        ]
        case_ids = {c["baseline_case_id"] for c in pair["cases"]} | {
            c["candidate_case_id"] for c in pair["cases"]
        }
        assert len(case_ids) == 2 * len(CHOSEN)

        # What the runs will execute, not only what they pin: case by case, the
        # same scenario version, twin definition and seed on both sides.
        executed = {}
        for run in (base, cand):
            detail = await s.ok("GET", f"/api/v1/simulations/{run['id']}")
            executed[run["side"]] = [
                (c["position"], c["scenario_version_id"], c["twin_definition_id"], c["seed"])
                for c in detail["cases"]
            ]
        assert executed["BASELINE"] == executed["CANDIDATE"]
        assert [seed for *_, seed in executed["BASELINE"]] == [pinned[n] for n in IN_RUN_ORDER]

        # Both runs are listed under the evaluation and announced to the workers.
        listed = await s.ok("GET", "/api/v1/simulations", project_id=PROJECT, eval_run_id=eval_run)
        assert sorted(r["side"] for r in listed["items"]) == ["BASELINE", "CANDIDATE"]
        requested = [e["payload"] for e in await s.outbox("simulation.run_requested.v1")]
        assert sorted((r["run_id"], r["eval_run_id"], r["side"]) for r in requested) == sorted(
            [(base["id"], eval_run, "BASELINE"), (cand["id"], eval_run, "CANDIDATE")]
        )

        # Run as any other run; the verdicts are the demo's (1.3.0 cuts corners).
        assert {await s.worker.process_next(), await s.worker.process_next()} == {base["id"], cand["id"]}
        verdicts = {}
        for run in (base, cand):
            detail = await s.ok("GET", f"/api/v1/simulations/{run['id']}")
            assert detail["run"]["status"] == "COMPLETED"
            verdicts[run["side"]] = {c["scenario_name"]: c["status"] for c in detail["cases"]}
        assert verdicts == {
            "BASELINE": {"refund-happy-path": "PASSED", "refund-timeout-after-mutation": "PASSED"},
            "CANDIDATE": {"refund-happy-path": "FAILED", "refund-timeout-after-mutation": "FAILED"},
        }
        completed = [e["payload"] for e in await s.outbox("simulation.run_completed.v1")]
        assert sorted((c["run_id"], c["eval_run_id"], c["side"]) for c in completed) == sorted(
            [(base["id"], eval_run, "BASELINE"), (cand["id"], eval_run, "CANDIDATE")]
        )


async def test_repeating_the_request_answers_the_same_pair() -> None:
    async with simulation_stack() as s:
        await s.register_suite()
        eval_run = str(uuid.uuid4())
        first = (await ask(s, pair_body(eval_run))).json()
        # The selection's order and duplicates do not change which scenarios run.
        again = await ask(s, pair_body(eval_run, scenarios=[*reversed(CHOSEN), CHOSEN[0]]))
        assert again.status_code == 200, again.text
        replay = again.json()
        assert replay["created"] is False
        for key in ("eval_run_id", "seed", "cases"):
            assert replay[key] == first[key]
        assert (replay["baseline"]["id"], replay["candidate"]["id"]) == (
            first["baseline"]["id"],
            first["candidate"]["id"],
        )
        # A different request for the same evaluation run is a conflict, not a second pair.
        other = await ask(s, pair_body(eval_run, candidate_version="1.3.1"))
        assert other.status_code == 409, other.text
        err = other.json()["error"]
        assert err["code"] == "PAIR_CONFLICT"
        assert err["details"] == {
            "baseline_run_id": first["baseline"]["id"],
            "candidate_run_id": first["candidate"]["id"],
        }
        assert len(await runs_of(s, eval_run)) == 2


async def test_concurrent_requests_create_one_pair() -> None:
    async with simulation_stack() as s:
        await s.register_suite()
        eval_run = str(uuid.uuid4())
        answers = await asyncio.gather(*(ask(s, pair_body(eval_run)) for _ in range(5)))
        statuses = sorted(a.status_code for a in answers)
        assert statuses == [200, 200, 200, 200, 201], [a.text for a in answers]
        ids = {(a.json()["baseline"]["id"], a.json()["candidate"]["id"]) for a in answers}
        assert len(ids) == 1
        assert [r["side"] for r in await runs_of(s, eval_run)] == ["BASELINE", "CANDIDATE"]
        requested = [
            e
            for e in await s.outbox("simulation.run_requested.v1")
            if e["payload"]["eval_run_id"] == eval_run
        ]
        assert len(requested) == 2
        pairs = await s.store.all("SELECT eval_run_id FROM simulation_pair")
        assert [p["eval_run_id"] for p in pairs] == [eval_run]


async def test_only_services_ask_and_only_for_their_projects() -> None:
    async with simulation_stack() as s:
        await s.register_suite()
        body = pair_body(str(uuid.uuid4()))
        engineer = await ask(s, body, as_=s.principal)
        assert (engineer.status_code, engineer.json()["error"]["code"]) == (403, "FORBIDDEN")
        elsewhere = await ask(s, body, as_=service_principal("evaluation-service", ORG, OTHER_PROJECT))
        assert (elsewhere.status_code, elsewhere.json()["error"]["code"]) == (404, "NOT_FOUND")
        anonymous = await s.client.post(PAIRS, json=body)
        assert (anonymous.status_code, anonymous.json()["error"]["code"]) == (401, "UNAUTHENTICATED")
        # A service of another organization cannot reach a pair either.
        stranger = Principal(
            org_id="0190f3b4-0000-7000-8000-00000000000f",
            actor="service:evaluation-service",
            role=Role.SERVICE,
            project_ids=(PROJECT,),
        )
        created = await ask(s, body)
        assert created.status_code == 201
        assert (await ask(s, body, as_=stranger)).status_code == 404
        assert len(await runs_of(s, body["eval_run_id"])) == 2


async def test_a_pair_is_checked_like_a_run_and_a_refusal_claims_nothing() -> None:
    async with simulation_stack() as s:
        await s.register_suite()
        eval_run = str(uuid.uuid4())
        unknown = await ask(s, pair_body(eval_run, candidate_version="9.9.9"))
        assert unknown.status_code == 400
        assert (unknown.json()["error"]["code"], unknown.json()["error"]["details"]) == (
            "AGENT_VERSION_NOT_FOUND",
            {"side": "candidate"},
        )
        missing = await ask(s, pair_body(eval_run, scenarios=["no-such-scenario"]))
        assert (missing.status_code, missing.json()["error"]["code"]) == (400, "SCENARIO_NOT_FOUND")
        bad = await ask(s, pair_body(eval_run, extra=1))
        assert (bad.status_code, bad.json()["error"]["code"]) == (400, "INVALID_REQUEST")
        not_uuid = await ask(s, pair_body("not-a-uuid"))
        assert (not_uuid.status_code, not_uuid.json()["error"]["details"]) == (400, {"field": "eval_run_id"})
        assert await runs_of(s, eval_run) == []
        assert await s.store.all("SELECT eval_run_id FROM simulation_pair") == []
        # Nothing was claimed: the evaluation run can still get its pair.
        assert (await ask(s, pair_body(eval_run))).status_code == 201


async def test_the_same_version_twice_is_a_valid_pair() -> None:
    async with simulation_stack() as s:
        await s.register_suite()
        resp = await ask(s, pair_body(str(uuid.uuid4()), candidate_version="1.2.4"))
        assert resp.status_code == 201
        pair = resp.json()
        assert pair["baseline"]["pinning"]["agent"] == pair["candidate"]["pinning"]["agent"]


async def test_public_runs_cannot_join_an_evaluation() -> None:
    async with simulation_stack() as s:
        await s.register_suite()
        base = {"project_id": PROJECT, "agent": AGENT, "agent_version": "1.2.4", "scenarios": CHOSEN}
        for extra, field in (
            ({"eval_run_id": str(uuid.uuid4())}, "eval_run_id"),
            ({"side": "BASELINE"}, "side"),
        ):
            resp = await s.call("POST", "/api/v1/simulations", base | extra)
            assert resp.status_code == 400, resp.text
            assert (resp.json()["error"]["code"], resp.json()["error"]["details"]) == (
                "INVALID_PARAMETER",
                {"field": field},
            )
        single = await s.ok("POST", "/api/v1/simulations", base | {"side": "SINGLE"}, status=202)
        assert (single["run"]["side"], single["run"]["eval_run_id"]) == ("SINGLE", None)
        assert "pair" not in single["run"]["pinning"]
