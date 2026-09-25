"""Runs a release asks for (``evaluation.run_requested.v1``, spec §28): the
evaluation service over the real simulation service and the real demo agent,
on PostgreSQL, every exchange checked against the evaluation, simulation and
trace contracts, every event against its published schema."""

from __future__ import annotations

import copy
import json
import uuid
from typing import Any

import pytest

from agenttwin_core.auth import Principal, Role
from agenttwin_core.events import Envelope, Permanent, validate_envelope
from agenttwin_core.yamlsafe import load_yaml
from agenttwin_evaluation.clients import PairRefused
from agenttwin_evaluation.worker import EMPTY_SUITE, judge_spend_limit
from eval_testutil import (
    ASSURANCE,
    ORG,
    OTHER_PROJECT,
    PROJECT,
    Stack,
    evaluation_stack,
    evaluation_with_simulation,
    run_simulations,
)
from sim_testutil import Stack as SimStack
from sim_testutil import scenario_yaml

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

AGENT = "support-refund-agent"


def agent_ref(version: str) -> dict[str, Any]:
    return {
        "agent": AGENT,
        "version": version,
        "version_id": str(uuid.uuid4()),
        "manifest_hash": f"sha256:{version}",
    }


def request(
    suite: list[dict[str, Any]],
    *,
    release_evaluation_id: str | None = None,
    project_id: str | None = PROJECT,
    baseline: str = "1.2.4",
    candidate: str = "1.3.0",
    **extra: Any,
) -> Envelope:
    """``evaluation.run_requested.v1`` as the control plane writes it; the
    envelope is checked against the published schemas."""
    payload = {
        "release_evaluation_id": release_evaluation_id or str(uuid.uuid4()),
        "release_id": str(uuid.uuid4()),
        "baseline": agent_ref(baseline),
        "candidate": agent_ref(candidate),
        "suite": suite,
    } | extra
    env = Envelope.new("evaluation.run_requested.v1", "control-plane", ORG, project_id, None, payload)
    return validate_envelope(env.to_dict())


def entry(name: str, version_id: str, **extra: Any) -> dict[str, Any]:
    reason = {"kind": "graph", "detail": f"{name} tests refund_payment"}
    return {"scenario_version_id": version_id, "scenario_name": name, "reasons": [reason]} | extra


async def versions(sim: SimStack, name: str) -> list[str]:
    """The ids of a scenario's versions, oldest first."""
    listed = await sim.ok("GET", "/api/v1/scenarios", project_id=PROJECT)
    sid = next(sc["id"] for sc in listed["items"] if sc["name"] == name)
    detail = await sim.ok("GET", f"/api/v1/scenarios/{sid}")
    return [v["id"] for v in sorted(detail["versions"], key=lambda v: v["version"])]


async def run_of(ev: Stack, release_evaluation_id: str) -> list[dict[str, Any]]:
    out = await ev.ok("GET", "/api/v1/eval-runs", release_evaluation_id=release_evaluation_id)
    items: list[dict[str, Any]] = out["items"]
    return items


def semantic_scenario(name: str) -> str:
    """The happy path with one semantic expectation the judge grades."""
    doc = load_yaml((ASSURANCE / "scenarios" / "refund-happy-path.yaml").read_text())
    doc["metadata"]["name"] = name
    doc["spec"]["expectations"] = [e for e in doc["spec"]["expectations"] if e["type"] != "semantic"]
    doc["spec"]["expectations"].append(
        {"type": "semantic", "rubric": "The reply confirms the refund.", "critical": False}
    )
    return json.dumps(doc)


async def test_a_release_runs_exactly_the_scenario_versions_it_selected() -> None:
    async with evaluation_with_simulation() as (ev, sim):
        happy_v1 = (await versions(sim, "refund-happy-path"))[0]
        timeout = (await versions(sim, "refund-timeout-after-mutation"))[0]
        lie = (await versions(sim, "refund-tool-success-lie"))[0]
        release_evaluation = str(uuid.uuid4())
        env = request(
            [
                entry("refund-timeout-after-mutation", timeout, severity="critical", mandatory=True),
                entry("refund-happy-path", happy_v1.upper(), severity="high"),
                entry("refund-tool-success-lie", lie, known_regression=True),
            ],
            release_evaluation_id=release_evaluation,
            seed=42,
            requested_by="user:release-manager",
        )
        await ev.worker.on_event(env)

        [run] = await run_of(ev, release_evaluation)
        assert (run["status"], run["seed"], run["requested_by"], run["release_id"]) == (
            "QUEUED",
            42,
            "user:release-manager",
            env.payload["release_id"],
        )
        assert run["release_evaluation_id"] == release_evaluation
        assert run["selection"] == {
            "scenarios": ["refund-happy-path", "refund-timeout-after-mutation", "refund-tool-success-lie"],
            "tags": None,
            "dataset": None,
            "scenario_versions": sorted([happy_v1, timeout, lie]),
        }

        # The scenario is edited after the release selected it: the release
        # still runs the version it selected.
        edited = scenario_yaml("refund-happy-path").replace(
            "description: >", "description: >\n    Edited after the release selected it.", 1
        )
        await sim.ok("POST", "/api/v1/scenarios", {"project_id": PROJECT, "yaml": edited}, status=201)
        assert (await versions(sim, "refund-happy-path"))[1] != happy_v1

        assert await ev.worker.process_next() == run["id"]
        running = (await ev.ok("GET", f"/api/v1/eval-runs/{run['id']}"))["run"]
        assert running["status"] == "RUNNING" and running["pinning"]["seed"] == 42
        for sim_id in (running["baseline_run_id"], running["candidate_run_id"]):
            detail = await sim.ok("GET", f"/api/v1/simulations/{sim_id}")
            assert {c["scenario_name"]: c["scenario_version_id"] for c in detail["cases"]} == {
                "refund-happy-path": happy_v1,
                "refund-timeout-after-mutation": timeout,
                "refund-tool-success-lie": lie,
            }
            assert detail["run"]["requested_by"] == "user:release-manager"
            assert detail["run"]["release_id"] == env.payload["release_id"]

        await run_simulations(sim)
        await ev.worker.check_waiting()
        out = await ev.ok("GET", f"/api/v1/eval-runs/{run['id']}")
        assert out["run"]["status"] == "COMPLETED", out["run"]["error"]
        classes = {c["scenario_name"]: c["classification"] for c in out["cases"]}
        assert classes == {
            "refund-happy-path": "REGRESSED",
            "refund-timeout-after-mutation": "NEW_CRITICAL_FAILURE",
            "refund-tool-success-lie": "NEW_CRITICAL_FAILURE",
        }
        [done] = await ev.outbox("evaluation.run_completed.v1")
        validate_envelope(done)
        assert done["payload"] == {
            "eval_run_id": run["id"],
            "release_evaluation_id": release_evaluation,
            "status": "COMPLETED",
            "error": None,
        }
        assert done["project_id"] == PROJECT and done["correlation_id"] == run["id"]


async def test_a_release_evaluation_gets_its_run_once() -> None:
    async with evaluation_with_simulation() as (ev, sim):
        happy = (await versions(sim, "refund-happy-path"))[0]
        release_evaluation = str(uuid.uuid4())
        env = request([entry("refund-happy-path", happy)], release_evaluation_id=release_evaluation)
        first = await ev.worker.release_requested(env)
        assert first is not None
        # Redelivered, and asked again under another event id: the same run.
        assert await ev.worker.release_requested(env) is None
        again = request([entry("refund-happy-path", happy)], release_evaluation_id=release_evaluation)
        assert again.id != env.id
        await ev.worker.on_event(again)
        [run] = await run_of(ev, release_evaluation)
        assert run["id"] == str(first["id"])
        # Nobody named: the control plane asked.
        assert (run["requested_by"], run["seed"]) == ("service:control-plane", None)
        transitions = (await ev.ok("GET", f"/api/v1/eval-runs/{run['id']}"))["transitions"]
        assert [(t["to_status"], t["reason"]) for t in transitions] == [
            ("QUEUED", f"requested by release evaluation {release_evaluation}")
        ]
        # Another release evaluation is another run; the API's runs are untouched by the index.
        await ev.worker.on_event(request([entry("refund-happy-path", happy)]))
        await ev.ok(
            "POST",
            "/api/v1/eval-runs",
            {
                "project_id": PROJECT,
                "agent": AGENT,
                "baseline_version": "1.2.4",
                "candidate_version": "1.3.0",
            },
            status=202,
        )
        await ev.ok(
            "POST",
            "/api/v1/eval-runs",
            {
                "project_id": PROJECT,
                "agent": AGENT,
                "baseline_version": "1.2.4",
                "candidate_version": "1.3.0",
            },
            status=202,
        )
        listed = (await ev.ok("GET", "/api/v1/eval-runs", project_id=PROJECT))["items"]
        assert len(listed) == 4
        assert sum(1 for r in listed if r["release_evaluation_id"] is None) == 2
        assert all(
            r["selection"]["scenario_versions"] is None for r in listed if r["release_evaluation_id"] is None
        )
        assert await run_of(ev, str(uuid.uuid4())) == []
        await ev.fails(
            "GET",
            "/api/v1/eval-runs",
            status=400,
            code="INVALID_PARAMETER",
            release_evaluation_id="not-a-uuid",
        )


async def test_a_release_that_selected_nothing_is_answered() -> None:
    async with evaluation_stack() as ev:
        release_evaluation = str(uuid.uuid4())
        empty = request([], release_evaluation_id=release_evaluation)
        await ev.worker.on_event(empty)
        # Redelivered: still one run and one answer.
        await ev.worker.on_event(empty)
        [run] = await run_of(ev, release_evaluation)
        assert (run["status"], run["error"], run["case_count"]) == ("FAILED", EMPTY_SUITE, 0)
        assert await ev.worker.process_next() is None
        [done] = await ev.outbox("evaluation.run_completed.v1")
        validate_envelope(done)
        assert done["payload"] == {
            "eval_run_id": run["id"],
            "release_evaluation_id": release_evaluation,
            "status": "FAILED",
            "error": EMPTY_SUITE,
        }
        # Nothing was asked of the simulation service.
        assert ev.simulation.calls == []


async def test_versions_the_simulation_service_does_not_have_fail_the_run_with_the_reason() -> None:
    async with evaluation_with_simulation() as (ev, sim):
        happy = (await versions(sim, "refund-happy-path"))[0]
        gone = str(uuid.uuid4())
        release_evaluation = str(uuid.uuid4())
        await ev.worker.on_event(
            request(
                [entry("refund-happy-path", happy), entry("refund-deleted", gone)],
                release_evaluation_id=release_evaluation,
            )
        )
        [run] = await run_of(ev, release_evaluation)
        assert await ev.worker.process_next() == run["id"]
        failed = (await ev.ok("GET", f"/api/v1/eval-runs/{run['id']}"))["run"]
        assert failed["status"] == "FAILED" and "The simulations could not start" in failed["error"]
        assert failed["error"].endswith(f"[missing versions: {gone}]"), failed["error"]
        [done] = await ev.outbox("evaluation.run_completed.v1")
        assert done["payload"]["release_evaluation_id"] == release_evaluation
        assert done["payload"]["status"] == "FAILED"
        assert await run_simulations(sim) == []


async def test_a_release_budget_caps_what_the_judge_may_spend() -> None:
    async with evaluation_with_simulation(judge_budget_usd=5.0) as (ev, sim):
        await sim.ok(
            "POST",
            "/api/v1/scenarios",
            {"project_id": PROJECT, "yaml": semantic_scenario("judged")},
            status=201,
        )
        judged = (await versions(sim, "judged"))[0]
        budgets = {}
        for gate_budget in (0.5, 50.0, None):
            release_evaluation = str(uuid.uuid4())
            extra = {"budget": {"max_gate_cost_usd": gate_budget}} if gate_budget is not None else {}
            await ev.worker.on_event(
                request([entry("judged", judged)], release_evaluation_id=release_evaluation, **extra)
            )
            [run] = await run_of(ev, release_evaluation)
            assert await ev.worker.process_next() == run["id"]
            await run_simulations(sim)
            await ev.worker.check_waiting()
            out = (await ev.ok("GET", f"/api/v1/eval-runs/{run['id']}"))["run"]
            assert out["status"] == "COMPLETED", out["error"]
            budgets[gate_budget] = out["budget"]["max_cost_usd"]
        # The release lowers the service's limit; it never raises it.
        assert budgets == {0.5: 0.5, 50.0: 5.0, None: 5.0}


def test_a_refusal_names_what_is_missing() -> None:
    ids = [f"v{i}" for i in range(12)]
    refused = PairRefused(400, "SCENARIO_NOT_FOUND", "Some versions do not exist.", {"missing_versions": ids})
    assert refused.describe() == (
        "SCENARIO_NOT_FOUND: Some versions do not exist. "
        "[missing versions: v0, v1, v2, v3, v4, v5, v6, v7, v8, v9 and 2 more]"
    )
    names = PairRefused(400, "SCENARIO_NOT_FOUND", "No such scenarios.", {"missing": ["a", "b"]})
    assert names.describe() == "SCENARIO_NOT_FOUND: No such scenarios. [missing scenarios: a, b]"
    side = PairRefused(400, "VERSION_NOT_FOUND", "No such version.", {"side": "candidate", "missing": []})
    assert side.describe() == "VERSION_NOT_FOUND: No such version. (candidate)"
    ten = PairRefused(400, "SCENARIO_NOT_FOUND", "x", {"missing_versions": ids[:10]})
    assert ten.describe().endswith("v9]")


def test_the_judges_limit_is_the_lower_of_the_two() -> None:
    assert judge_spend_limit(5.0, 0.5) == 0.5
    assert judge_spend_limit(5.0, 50.0) == 5.0
    assert judge_spend_limit(5.0, None) == 5.0
    assert judge_spend_limit(None, 0.5) == 0.5
    assert judge_spend_limit(None, None) is None
    assert judge_spend_limit(5.0, 0.0) == 0.0


def _broken(mutate: str) -> Envelope:
    version = str(uuid.uuid4())
    env = request([entry("refund-happy-path", version)])
    raw = copy.deepcopy(env.to_dict())
    payload = raw["payload"]
    if mutate == "two agents":
        payload["candidate"]["agent"] = "other-agent"
    elif mutate == "no project":
        raw.pop("project_id", None)
    elif mutate == "project not a uuid":
        raw["project_id"] = "project-1"
    elif mutate == "agent name":
        payload["baseline"]["agent"] = payload["candidate"]["agent"] = "Support Agent"
    elif mutate == "empty version":
        payload["candidate"]["version"] = ""
    elif mutate == "version too long":
        payload["baseline"]["version"] = "1" * 101
    elif mutate == "version id":
        payload["suite"][0]["scenario_version_id"] = "v1"
    elif mutate == "scenario name":
        payload["suite"][0]["scenario_name"] = "Refund Happy Path"
    elif mutate == "release evaluation id":
        payload["release_evaluation_id"] = "re-1"
    elif mutate == "boolean seed":
        payload["seed"] = True
    elif mutate == "negative seed":
        payload["seed"] = -1
    elif mutate == "seed too large":
        payload["seed"] = 2**32
    elif mutate == "negative budget":
        payload["budget"] = {"max_gate_cost_usd": -1}
    elif mutate == "boolean budget":
        payload["budget"] = {"max_gate_cost_usd": True}
    elif mutate == "suite missing":
        payload.pop("suite")
    else:
        raise AssertionError(mutate)
    return Envelope.from_dict(raw)


BROKEN = (
    "two agents",
    "no project",
    "project not a uuid",
    "agent name",
    "empty version",
    "version too long",
    "version id",
    "scenario name",
    "release evaluation id",
    "boolean seed",
    "negative seed",
    "seed too large",
    "negative budget",
    "boolean budget",
    "suite missing",
)


async def test_requests_that_cannot_describe_a_run_are_parked_and_create_nothing() -> None:
    async with evaluation_stack() as ev:
        for mutate in BROKEN:
            with pytest.raises(Permanent):
                await ev.worker.on_event(_broken(mutate))
        listed = await ev.ok("GET", "/api/v1/eval-runs", project_id=PROJECT)
        assert listed["items"] == []
        assert await ev.outbox("evaluation.run_completed.v1") == []
        # The well-formed request next to them is accepted.
        await ev.worker.on_event(request([entry("refund-happy-path", str(uuid.uuid4()))]))
        assert len((await ev.ok("GET", "/api/v1/eval-runs", project_id=PROJECT))["items"]) == 1


async def test_a_release_run_stays_in_its_project() -> None:
    async with evaluation_stack() as ev:
        release_evaluation = str(uuid.uuid4())
        await ev.worker.on_event(
            request([entry("refund-happy-path", str(uuid.uuid4()))], release_evaluation_id=release_evaluation)
        )
        outsider = Principal(
            org_id=ORG, actor="user:outsider", role=Role.ENGINEER, project_ids=(OTHER_PROJECT,)
        )
        out = await ev.ok("GET", "/api/v1/eval-runs", as_=outsider, release_evaluation_id=release_evaluation)
        assert out["items"] == []
        [run] = await run_of(ev, release_evaluation)
        assert run["project_id"] == PROJECT and run["organization_id"] == ORG
