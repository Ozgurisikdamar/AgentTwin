"""The regressions API on PostgreSQL (spec §18, ADR-0032): the inbox of
mined failures, triage, status changes, merges, the scenario draft of a
failure and its promotion into a regression test — through the real
simulation service where a scenario is drafted or registered. Every
exchange is checked against the contract."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from agenttwin_core.auth import Principal, Role
from agenttwin_core.db import transaction
from agenttwin_core.yamlsafe import dump_yaml, load_yaml
from agenttwin_evaluation.regressions import DATASET
from eval_testutil import ORG, OTHER_PROJECT, PROJECT, Stack, evaluation_stack, evaluation_with_simulation
from regression_testutil import AGENT, ingested, load, mine, mined, variant

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

BASE = "/api/v1/regressions"
REVIEWER = Principal(org_id=ORG, actor="user:reviewer", role=Role.REVIEWER, project_ids=(PROJECT,))
VIEWER = Principal(org_id=ORG, actor="user:viewer", role=Role.VIEWER, project_ids=(PROJECT,))
CI_KEY = Principal(org_id=ORG, actor="apikey:ci", role=Role.API_KEY, project_ids=(PROJECT,), scopes=("ci",))


async def audits(ev: Stack) -> list[dict[str, Any]]:
    return [e["payload"] for e in await ev.outbox("audit.recorded.v1")]


def history(detail: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [(e["action"], e["from_status"], e["to_status"], e["actor"]) for e in detail["events"]]


# ---------------------------------------------------------------- the inbox


async def test_the_inbox_lists_the_projects_groups_most_recently_seen_first() -> None:
    async with evaluation_stack() as ev:
        dup, _ = await mined(ev, "duplicate-refund")
        denied, _ = await mined(ev, "cross-tenant-denied")
        handled, _ = await mined(ev, "timeout-handled")
        # Another project of the organization the caller cannot see.
        await mined(ev, "duplicate-refund", project=OTHER_PROJECT, trace_id=uuid.uuid4().hex)
        page = await ev.ok("GET", f"{BASE}/candidates")
        assert [r["id"] for r in page["items"]] == [denied, dup, handled]
        assert page["next_cursor"] is None
        first = page["items"][1]
        assert (first["agent"], first["status"], first["taxonomy"], first["severity"]) == (
            AGENT,
            "CANDIDATE",
            "DUPLICATE_SIDE_EFFECT",
            "critical",
        )
        assert (first["occurrence_count"], first["versions"], first["project_id"]) == (1, ["1.3.0"], PROJECT)
        assert first["representative_trace_id"] == "f310c0de303dcad73b1f5f0b10509536"
        assert first["scenario_name"] is None and first["merged_into"] is None

        async def ids(query: str) -> list[str]:
            # In the URL: ``status`` is also the helpers' expected HTTP status.
            return [r["id"] for r in (await ev.ok("GET", f"{BASE}/candidates?{query}"))["items"]]

        assert await ids("severity=critical") == [dup]
        assert await ids("severity=critical,medium") == [dup, handled]
        assert await ids("taxonomy=AUTHORIZATION") == [denied]
        assert await ids("status=CANDIDATE,CONFIRMED") == [denied, dup, handled]
        assert await ids("status=CONFIRMED") == []
        assert await ids("agent=another-agent") == []
        assert await ids(f"agent={AGENT}&project_id={PROJECT}") == [denied, dup, handled]
        # Pages of one: the cursor walks the same order.
        walked: list[str] = []
        cursor: str | None = None
        while True:
            page = await ev.ok("GET", f"{BASE}/candidates", limit=1, cursor=cursor)
            walked += [r["id"] for r in page["items"]]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert walked == [denied, dup, handled]
        # Only projects of the caller.
        await ev.fails("GET", f"{BASE}/candidates", status=404, code="NOT_FOUND", project_id=OTHER_PROJECT)
        for query, field in (
            ("status=CANDIDATE,OPEN", "status"),
            ("severity=urgent", "severity"),
            ("taxonomy=BAD", "taxonomy"),
            ("include_merged=yes", "include_merged"),
            ("agent=" + "a" * 201, "agent"),
        ):
            error = await ev.fails("GET", f"{BASE}/candidates?{query}", status=400, code="INVALID_PARAMETER")
            assert error["details"]["field"] == field
        await ev.fails("GET", f"{BASE}/candidates", status=400, code="INVALID_CURSOR", cursor="nope")


async def test_a_regression_shows_its_failures_and_its_history() -> None:
    async with evaluation_stack() as ev:
        dup, first = await mined(ev, "duplicate-refund")
        await mined(ev, "duplicate-refund", trace_id=uuid.uuid4().hex, started_at="2026-09-25T17:00:00Z")
        detail = await ev.ok("GET", f"{BASE}/{dup}")
        assert detail["regression"]["occurrence_count"] == 2
        [newest, oldest] = detail["occurrences"]
        assert oldest["trace_id"] == first["trace"]["trace_id"] and oldest["join_kind"] == "new"
        assert newest["join_kind"] == "exact" and newest["started_at"] > oldest["started_at"]
        assert oldest["reasons"] and oldest["evidence"] and oldest["severity"] == "critical"
        # Structured features only: no conversation in the answer.
        assert "arrived broken" not in str(detail)
        assert history(detail) == [("created", None, "CANDIDATE", "system:regression-miner")]
        # Viewers read.
        await ev.ok("GET", f"{BASE}/{dup}", as_=VIEWER)
        await ev.fails("GET", f"{BASE}/{uuid.uuid4()}", status=404, code="NOT_FOUND")
        await ev.fails("GET", f"{BASE}/not-a-uuid", status=400, code="INVALID_PARAMETER")
        other, _ = await mined(ev, "duplicate-refund", project=OTHER_PROJECT, trace_id=uuid.uuid4().hex)
        await ev.fails("GET", f"{BASE}/{other}", status=404, code="NOT_FOUND")
        await ev.fails("POST", f"{BASE}/{other}/confirm", {}, status=404, code="NOT_FOUND")


# ---------------------------------------------------------------- people's decisions


async def test_people_confirm_dismiss_and_reopen_with_their_reasons() -> None:
    async with evaluation_stack() as ev:
        dup, _ = await mined(ev, "duplicate-refund")
        await ev.fails("POST", f"{BASE}/{dup}/confirm", {}, status=403, code="FORBIDDEN", as_=VIEWER)
        out = await ev.ok("POST", f"{BASE}/{dup}/confirm", {})
        assert out["regression"]["status"] == "CONFIRMED"
        error = await ev.fails(
            "POST", f"{BASE}/{dup}/confirm", {}, status=409, code="REGRESSION_TRANSITION_INVALID"
        )
        assert error["details"] == {"status": "CONFIRMED", "action": "confirm"}
        for action in ("dismiss", "reopen"):
            await ev.fails("POST", f"{BASE}/{dup}/{action}", {}, status=400, code="INVALID_REQUEST")
            await ev.fails(
                "POST", f"{BASE}/{dup}/{action}", {"reason": "  "}, status=400, code="INVALID_REQUEST"
            )
        await ev.fails(
            "POST", f"{BASE}/{dup}/confirm", {"reason": "x", "extra": 1}, status=400, code="INVALID_REQUEST"
        )
        await ev.fails(
            "POST",
            f"{BASE}/{dup}/reopen",
            {"reason": "back"},
            status=409,
            code="REGRESSION_TRANSITION_INVALID",
        )
        out = await ev.ok("POST", f"{BASE}/{dup}/dismiss", {"reason": "a test tenant"}, as_=REVIEWER)
        assert out["regression"]["status"] == "DISMISSED"
        out = await ev.ok("POST", f"{BASE}/{dup}/reopen", {"reason": "a real customer after all"})
        assert out["regression"]["status"] == "REOPENED"
        detail = await ev.ok("GET", f"{BASE}/{dup}")
        assert history(detail) == [
            ("created", None, "CANDIDATE", "system:regression-miner"),
            ("confirm", "CANDIDATE", "CONFIRMED", "user:engineer"),
            ("dismiss", "CONFIRMED", "DISMISSED", "user:reviewer"),
            ("reopen", "DISMISSED", "REOPENED", "user:engineer"),
        ]
        assert [e["reason"] for e in detail["events"][1:]] == [
            None,
            "a test tenant",
            "a real customer after all",
        ]
        assert [(a["action"], a["resource_id"], a["reason"], a["metadata"]) for a in await audits(ev)] == [
            ("regression.confirm", dup, None, {"from": "CANDIDATE", "to": "CONFIRMED"}),
            ("regression.dismiss", dup, "a test tenant", {"from": "CONFIRMED", "to": "DISMISSED"}),
            ("regression.reopen", dup, "a real customer after all", {"from": "DISMISSED", "to": "REOPENED"}),
        ]


async def test_triage_sets_the_label_severity_tags_and_assignee_and_the_miner_keeps_them() -> None:
    async with evaluation_stack() as ev:
        dup, _ = await mined(ev, "duplicate-refund")
        body = {
            "taxonomy": "RETRY_SAFETY",
            "severity": "high",
            "tags": ["payments", "refunds", "payments"],
            "assignee": "user:alex",
            "reason": "retries are the root cause",
        }
        await ev.fails("PATCH", f"{BASE}/{dup}", body, status=403, code="FORBIDDEN", as_=VIEWER)
        g = (await ev.ok("PATCH", f"{BASE}/{dup}", body))["regression"]
        assert (g["taxonomy"], g["suggested_taxonomy"], g["severity"], g["suggested_severity"]) == (
            "RETRY_SAFETY",
            "DUPLICATE_SIDE_EFFECT",
            "high",
            "critical",
        )
        assert (g["tags"], g["assignee"], g["triaged_by"]) == (
            ["payments", "refunds"],
            "user:alex",
            "user:engineer",
        )
        assert g["severity_reason"] == "set by user:engineer" and g["status"] == "CANDIDATE"
        # The same values again change nothing and record nothing.
        again = (await ev.ok("PATCH", f"{BASE}/{dup}", body))["regression"]
        assert again["updated_at"] == g["updated_at"]
        # Unassigning is a change of its own.
        g = (await ev.ok("PATCH", f"{BASE}/{dup}", {"assignee": None}))["regression"]
        assert g["assignee"] is None and g["taxonomy"] == "RETRY_SAFETY"
        detail = await ev.ok("GET", f"{BASE}/{dup}")
        assert [(e["action"], e["reason"], e["detail"]) for e in detail["events"][1:]] == [
            (
                "triage",
                "retries are the root cause",
                {
                    "taxonomy": ["DUPLICATE_SIDE_EFFECT", "RETRY_SAFETY"],
                    "severity": ["critical", "high"],
                    "tags": [[], ["payments", "refunds"]],
                },
            ),
            ("assign", "retries are the root cause", {"assignee": [None, "user:alex"]}),
            ("assign", None, {"assignee": ["user:alex", None]}),
        ]
        [first, second] = await audits(ev)
        assert (first["action"], first["reason"]) == ("regression.triage", "retries are the root cause")
        assert first["metadata"]["changes"]["severity"] == ["critical", "high"]
        assert first["before_hash"] != first["after_hash"]
        assert second["metadata"] == {"changes": {"assignee": ["user:alex", None]}}
        # A person's label and severity survive the next failure of the group.
        await mined(ev, "duplicate-refund", trace_id=uuid.uuid4().hex)
        g = (await ev.ok("GET", f"{BASE}/{dup}"))["regression"]
        assert (g["occurrence_count"], g["taxonomy"], g["severity"]) == (2, "RETRY_SAFETY", "high")
        for bad, code in (
            ({}, "INVALID_REQUEST"),
            ({"reason": "only a reason"}, "INVALID_REQUEST"),
            ({"taxonomy": "SOMETHING"}, "INVALID_PARAMETER"),
            ({"severity": None}, "INVALID_PARAMETER"),
            ({"severity": "urgent"}, "INVALID_REQUEST"),
            ({"tags": ["Not A Tag"]}, "INVALID_PARAMETER"),
            ({"tags": [f"t{i}" for i in range(21)]}, "INVALID_REQUEST"),
            ({"assignee": "alex"}, "INVALID_PARAMETER"),
            ({"owner": "user:alex"}, "INVALID_REQUEST"),
        ):
            await ev.fails("PATCH", f"{BASE}/{dup}", bad, status=400, code=code)


async def test_merging_moves_failures_and_fingerprints_into_the_other_group() -> None:
    async with evaluation_stack() as ev:
        dup, _ = await mined(ev, "duplicate-refund")
        loop, _ = await mined(ev, "timeout-handled")
        denied, _ = await mined(ev, "cross-tenant-denied")
        assert len({dup, loop, denied}) == 3
        await ev.fails(
            "POST", f"{BASE}/{loop}/merge", {"into": dup}, status=403, code="FORBIDDEN", as_=VIEWER
        )
        await ev.fails("POST", f"{BASE}/{loop}/merge", {"into": loop}, status=400, code="INVALID_REQUEST")
        await ev.fails("POST", f"{BASE}/{loop}/merge", {"into": "x"}, status=400, code="INVALID_PARAMETER")
        await ev.fails(
            "POST", f"{BASE}/{loop}/merge", {"into": str(uuid.uuid4())}, status=404, code="NOT_FOUND"
        )
        other, _ = await mined(ev, "duplicate-refund", project=OTHER_PROJECT, trace_id=uuid.uuid4().hex)
        await ev.fails("POST", f"{BASE}/{loop}/merge", {"into": other}, status=404, code="NOT_FOUND")
        # Not across projects, even for someone who can see both.
        both = Principal(
            org_id=ORG, actor="user:lead", role=Role.ENGINEER, project_ids=(PROJECT, OTHER_PROJECT)
        )
        await ev.fails(
            "POST", f"{BASE}/{loop}/merge", {"into": other}, status=404, code="NOT_FOUND", as_=both
        )

        out = await ev.ok("POST", f"{BASE}/{loop}/merge", {"into": dup, "reason": "the same double refund"})
        target, merged = out["regression"], out["merged"]
        assert (target["id"], target["occurrence_count"]) == (dup, 2)
        assert (merged["id"], merged["merged_into"], merged["occurrence_count"]) == (loop, dup, 0)
        page = await ev.ok("GET", f"{BASE}/candidates")
        assert loop not in [r["id"] for r in page["items"]]
        page = await ev.ok("GET", f"{BASE}/candidates", include_merged="true")
        assert loop in [r["id"] for r in page["items"]]
        # A new failure of the merged group's kind joins the one it was merged into.
        result = await mine(ev, ingested(variant(load("timeout-handled"))))
        assert result.group_id == dup
        assert history(await ev.ok("GET", f"{BASE}/{loop}"))[-1] == ("merged", None, None, "user:engineer")
        events = (await ev.ok("GET", f"{BASE}/{dup}"))["events"]
        assert (events[-1]["action"], events[-1]["detail"]) == ("merge", {"from": loop, "occurrences": 1})
        [a] = await audits(ev)
        assert (a["action"], a["resource_id"], a["metadata"]) == (
            "regression.merge",
            loop,
            {"into": dup, "occurrences": 1},
        )
        # A merged group is acted on through the one it joined.
        for path, body in (
            (f"{BASE}/{loop}/merge", {"into": denied}),
            (f"{BASE}/{denied}/merge", {"into": loop}),
            (f"{BASE}/{loop}/confirm", {}),
        ):
            await ev.fails("POST", path, body, status=409, code="REGRESSION_MERGED")
        await ev.fails("PATCH", f"{BASE}/{loop}", {"severity": "low"}, status=409, code="REGRESSION_MERGED")
        # Only groups of one agent, and a group that became a test stays.
        stranger, _ = await mined(
            ev, "cross-tenant-denied", agent_name="billing-agent", trace_id=uuid.uuid4().hex
        )
        await ev.fails(
            "POST", f"{BASE}/{stranger}/merge", {"into": denied}, status=409, code="REGRESSION_AGENT_MISMATCH"
        )
        async with transaction(ev.pool) as conn:
            await conn.execute(
                """UPDATE regression_group SET status = 'PROMOTED', scenario_name = 'already-a-test'
                   WHERE id = %s""",
                (denied,),
            )
        await ev.fails(
            "POST", f"{BASE}/{denied}/merge", {"into": dup}, status=409, code="REGRESSION_HAS_TEST"
        )


# ---------------------------------------------------------------- draft and promotion


async def test_the_draft_is_the_scenario_its_representative_trace_suggests() -> None:
    async with evaluation_with_simulation() as (ev, _):
        dup, detail = await mined(ev, "duplicate-refund")
        out = await ev.ok("GET", f"{BASE}/{dup}/draft", as_=VIEWER)
        assert (out["regression_id"], out["trace_id"]) == (dup, detail["trace"]["trace_id"])
        draft = out["draft"]
        assert draft["complete"] is True and draft["problems"] == []
        doc = draft["document"]
        assert load_yaml(draft["yaml"]) == doc
        assert (doc["kind"], doc["spec"]["agent"], doc["spec"]["twin"]) == (
            "Scenario",
            AGENT,
            "demo-co-support",
        )
        assert doc["metadata"]["generated"]["reviewed"] is False
        assert [m["target_id"] for m in draft["mappings"]] == ["ORD-1001"]
        # Nothing is saved by drafting.
        assert (await ev.ok("GET", f"{BASE}/{dup}"))["regression"]["status"] == "CANDIDATE"
        assert await ev.outbox("audit.recorded.v1") == []
        # A trace no longer kept cannot be drafted from.
        del ev.traces.details[detail["trace"]["trace_id"]]
        error = await ev.fails("GET", f"{BASE}/{dup}/draft", status=409, code="REGRESSION_TRACE_GONE")
        assert error["details"] == {"trace_id": detail["trace"]["trace_id"]}


async def test_a_draft_needs_the_services_it_reads() -> None:
    async with evaluation_stack() as ev:
        dup, _ = await mined(ev, "duplicate-refund")
        ev.simulation.fail = "down"
        await ev.fails("GET", f"{BASE}/{dup}/draft", status=503, code="UNAVAILABLE")
        await ev.fails("POST", f"{BASE}/{dup}/promote", {}, status=503, code="UNAVAILABLE", as_=REVIEWER)
        assert (await ev.ok("GET", f"{BASE}/{dup}"))["regression"]["status"] == "CANDIDATE"


async def test_promoting_makes_a_regression_test_every_future_evaluation_runs() -> None:
    async with evaluation_with_simulation() as (ev, sim):
        dup, detail = await mined(ev, "duplicate-refund")
        tid = detail["trace"]["trace_id"]
        # Engineers review; promoting is for reviewers, admins and owners — and never CI.
        for who in (ev.principal, VIEWER, CI_KEY):
            error = await ev.fails("POST", f"{BASE}/{dup}/promote", {}, status=403, code="FORBIDDEN", as_=who)
            assert error["details"]["required_permission"] == "regression.promote"
        out = await ev.ok(
            "POST",
            f"{BASE}/{dup}/promote",
            {"reason": "customers were refunded twice"},
            status=201,
            as_=REVIEWER,
        )
        g, scenario, dataset = out["regression"], out["scenario"], out["dataset"]
        assert g["status"] == "PROMOTED" and out["redacted"] == 0
        assert scenario["name"].startswith("regression-refund-payment-took-effect-twice-")
        assert (scenario["version"], scenario["created"]) == (1, True)
        assert (dataset["name"], dataset["version"]) == (DATASET, 1)
        assert (g["scenario_id"], g["scenario_name"], g["dataset_id"], g["dataset_version"]) == (
            scenario["id"],
            scenario["name"],
            dataset["id"],
            1,
        )
        assert g["promoted_by"] == "user:reviewer" and g["promoted_at"]
        # The scenario in the simulation service: a reviewed production regression of the trace.
        registered = await sim.ok("GET", f"/api/v1/scenarios/{scenario['id']}")
        meta = registered["document"]["metadata"]
        assert (meta["source"], meta["sourceTraceId"], meta["generated"]["reviewed"]) == (
            "production_regression",
            tid,
            True,
        )
        assert "production-regression" in meta["tags"]
        assert registered["document"]["spec"]["agent"] == AGENT
        # The case in the project's regression dataset.
        ds = await ev.ok("GET", f"/api/v1/datasets/{dataset['id']}")
        [case] = ds["version"]["cases"]
        assert (case["scenario"], case["source"], case["privacy"], case["trace_id"], case["added_by"]) == (
            scenario["name"],
            "production_regression",
            "redacted",
            tid,
            "user:reviewer",
        )
        assert ds["dataset"]["tags"] == ["production-regression"]
        # History and audit.
        events = (await ev.ok("GET", f"{BASE}/{dup}"))["events"]
        assert (events[-1]["action"], events[-1]["from_status"], events[-1]["to_status"]) == (
            "promote",
            "CANDIDATE",
            "PROMOTED",
        )
        assert events[-1]["reason"] == "customers were refunded twice"
        assert events[-1]["detail"] == {
            "scenario": scenario["name"],
            "scenario_version": 1,
            "dataset": DATASET,
            "dataset_version": 1,
            "redacted": 0,
        }
        assert [a["action"] for a in await audits(ev)] == ["dataset.create", "regression.promote"]
        # Promoted once: the test exists.
        await ev.fails(
            "POST",
            f"{BASE}/{dup}/promote",
            {},
            status=409,
            code="REGRESSION_TRANSITION_INVALID",
            as_=REVIEWER,
        )
        # The next promotion joins the same dataset as its next version.
        denied, _ = await mined(ev, "cross-tenant-denied")
        out = await ev.ok("POST", f"{BASE}/{denied}/promote", {}, status=201, as_=REVIEWER)
        assert (out["dataset"]["id"], out["dataset"]["version"]) == (dataset["id"], 2)
        ds = await ev.ok("GET", f"/api/v1/datasets/{dataset['id']}")
        assert sorted(c["scenario"] for c in ds["version"]["cases"]) == sorted(
            [scenario["name"], out["scenario"]["name"]]
        )
        assert [a["action"] for a in await audits(ev)][2:] == ["dataset.update", "regression.promote"]


async def test_a_promoted_document_is_the_persons_but_its_source_and_privacy_are_not() -> None:
    async with evaluation_with_simulation() as (ev, sim):
        dup, detail = await mined(ev, "duplicate-refund")
        draft = (await ev.ok("GET", f"{BASE}/{dup}/draft"))["draft"]["document"]
        doc = dict(draft)
        doc["metadata"] = dict(draft["metadata"]) | {
            "name": "double-refund-on-timeout",
            "source": "manual",
            "sourceTraceId": "0" * 32,
            "tags": ["refunds"],
        }
        doc["spec"] = dict(draft["spec"]) | {
            "input": {"message": "Refund ORD-1001 and mail jane.doe@example.com, card 4111 1111 1111 1111"}
        }

        async def refused(body: dict[str, Any], code: str = "SCENARIO_INVALID") -> dict[str, Any]:
            return await ev.fails("POST", f"{BASE}/{dup}/promote", body, status=400, code=code, as_=REVIEWER)

        await refused({"document": doc, "yaml": dump_yaml(doc)}, "INVALID_REQUEST")
        await refused({"yaml": "a: [b"})
        await refused({"document": doc | {"kind": "Twin"}})
        await refused({"document": doc | {"spec": doc["spec"] | {"agent": "billing-agent"}}})
        error = await refused({"document": doc | {"spec": doc["spec"] | {"expectations": "none"}}})
        assert error["details"]["errors"]
        # The simulation service's own refusal (the twin does not exist).
        error = await refused({"document": doc | {"spec": doc["spec"] | {"twin": "no-such-twin"}}})
        assert error["details"]["code"]
        assert (await ev.ok("GET", f"{BASE}/{dup}"))["regression"]["status"] == "CANDIDATE"

        out = await ev.ok("POST", f"{BASE}/{dup}/promote", {"yaml": dump_yaml(doc)}, status=201, as_=REVIEWER)
        assert out["scenario"]["name"] == "double-refund-on-timeout" and out["redacted"] == 1
        registered = (await sim.ok("GET", f"/api/v1/scenarios/{out['scenario']['id']}"))["document"]
        assert registered["spec"]["input"]["message"] == (
            "Refund ORD-1001 and mail [REDACTED:email], card [REDACTED:card]"
        )
        meta = registered["metadata"]
        assert (meta["source"], meta["sourceTraceId"]) == (
            "production_regression",
            detail["trace"]["trace_id"],
        )
        assert meta["tags"] == ["production-regression", "refunds"]
        ds = await ev.ok("GET", f"/api/v1/datasets/{out['dataset']['id']}")
        assert ds["version"]["cases"][0]["tags"] == ["production-regression", "refunds"]


async def test_a_draft_that_needs_a_person_is_not_promoted_as_it_is() -> None:
    async with evaluation_with_simulation() as (ev, _):
        dup, detail = await mined(ev, "duplicate-refund")
        for span in detail["spans"]:
            span["content"] = {}
        detail["trace"]["content_purged"] = True
        error = await ev.fails(
            "POST", f"{BASE}/{dup}/promote", {}, status=409, code="REGRESSION_DRAFT_INCOMPLETE", as_=REVIEWER
        )
        assert error["details"]["problems"][0].startswith(
            "The trace no longer shows what the agent was asked"
        )
        assert (await ev.ok("GET", f"{BASE}/{dup}"))["regression"]["status"] == "CANDIDATE"


async def test_a_promotion_interrupted_after_registering_its_scenario_is_completed_by_its_retry() -> None:
    async with evaluation_with_simulation() as (ev, _):
        dup, _ = await mined(ev, "duplicate-refund")
        real = ev.store.dataset_named
        calls = 0

        async def failing_once(*args: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("the database went away")
            return await real(*args)

        ev.store.dataset_named = failing_once  # type: ignore[method-assign]
        await ev.fails("POST", f"{BASE}/{dup}/promote", {}, status=500, code="INTERNAL", as_=REVIEWER)
        assert (await ev.ok("GET", f"{BASE}/{dup}"))["regression"]["status"] == "CANDIDATE"
        assert await ev.outbox("audit.recorded.v1") == []
        out = await ev.ok("POST", f"{BASE}/{dup}/promote", {}, status=201, as_=REVIEWER)
        # The scenario was registered by the first attempt: the retry finds it unchanged.
        assert (out["scenario"]["version"], out["scenario"]["created"]) == (1, False)
        assert out["dataset"]["version"] == 1
        ds = await ev.ok("GET", f"/api/v1/datasets/{out['dataset']['id']}")
        assert len(ds["version"]["cases"]) == 1


async def test_two_promotions_of_one_regression_make_one_test() -> None:
    async with evaluation_with_simulation() as (ev, _):
        dup, _ = await mined(ev, "duplicate-refund")
        answers = await asyncio.gather(
            *(ev.call("POST", f"{BASE}/{dup}/promote", {}, as_=REVIEWER) for _ in range(2))
        )
        assert sorted(a.status_code for a in answers) == [201, 409]
        [lost] = [a for a in answers if a.status_code == 409]
        assert lost.json()["error"]["code"] == "REGRESSION_TRANSITION_INVALID"
        events = (await ev.ok("GET", f"{BASE}/{dup}"))["events"]
        assert [e["action"] for e in events] == ["created", "promote"]
        [won] = [a.json() for a in answers if a.status_code == 201]
        ds = await ev.ok("GET", f"/api/v1/datasets/{won['dataset']['id']}")
        assert (ds["dataset"]["latest_version"], len(ds["version"]["cases"])) == (1, 1)


async def test_promotions_that_both_find_no_regression_dataset_share_the_one_created_first() -> None:
    async with evaluation_with_simulation() as (ev, _):
        dup, _ = await mined(ev, "duplicate-refund")
        # Another agent's failure: its own mining lock, so nothing orders the two.
        other, _ = await mined(
            ev, "cross-tenant-denied", agent_name="billing-agent", trace_id=uuid.uuid4().hex
        )
        real = ev.store.dataset_named
        barrier = asyncio.Barrier(2)
        calls = 0

        async def together(*args: Any) -> Any:
            # The first two lookups wait for each other: both find no dataset.
            nonlocal calls
            calls += 1
            if calls <= 2:
                await asyncio.wait_for(barrier.wait(), 20)
            return await real(*args)

        ev.store.dataset_named = together  # type: ignore[method-assign]
        answers = await asyncio.gather(
            *(ev.call("POST", f"{BASE}/{g}/promote", {}, as_=REVIEWER) for g in (dup, other))
        )
        assert [a.status_code for a in answers] == [201, 201], [a.text for a in answers]
        datasets = {a.json()["dataset"]["id"] for a in answers}
        assert len(datasets) == 1 and calls == 3
        assert sorted(a.json()["dataset"]["version"] for a in answers) == [1, 2]
        ds = await ev.ok("GET", f"/api/v1/datasets/{datasets.pop()}")
        assert sorted(c["scenario"] for c in ds["version"]["cases"]) == sorted(
            a.json()["scenario"]["name"] for a in answers
        )
        assert sorted(a["action"] for a in await audits(ev)) == [
            "dataset.create",
            "dataset.update",
            "regression.promote",
            "regression.promote",
        ]


async def test_a_scenario_promoted_again_keeps_one_case_the_latest() -> None:
    async with evaluation_with_simulation() as (ev, _):
        dup, _ = await mined(ev, "duplicate-refund")
        first = await ev.ok("POST", f"{BASE}/{dup}/promote", {}, status=201, as_=REVIEWER)
        name = first["scenario"]["name"]
        # A person covers another failure with the same test: a new version of it.
        denied, detail = await mined(ev, "cross-tenant-denied")
        doc = (await ev.ok("GET", f"{BASE}/{denied}/draft"))["draft"]["document"]
        doc["metadata"]["name"] = name
        out = await ev.ok("POST", f"{BASE}/{denied}/promote", {"document": doc}, status=201, as_=REVIEWER)
        assert (out["scenario"]["name"], out["scenario"]["version"], out["scenario"]["created"]) == (
            name,
            2,
            True,
        )
        assert (out["dataset"]["id"], out["dataset"]["version"]) == (first["dataset"]["id"], 2)
        ds = await ev.ok("GET", f"/api/v1/datasets/{out['dataset']['id']}")
        [case] = ds["version"]["cases"]
        assert (case["scenario"], case["trace_id"]) == (name, detail["trace"]["trace_id"])


async def test_an_archived_regression_dataset_takes_no_more_promotions() -> None:
    async with evaluation_with_simulation() as (ev, _):
        dup, _ = await mined(ev, "duplicate-refund")
        first = await ev.ok("POST", f"{BASE}/{dup}/promote", {}, status=201, as_=REVIEWER)
        await ev.ok("POST", f"/api/v1/datasets/{first['dataset']['id']}/archive")
        denied, _ = await mined(ev, "cross-tenant-denied")
        await ev.fails(
            "POST",
            f"{BASE}/{denied}/promote",
            {},
            status=409,
            code="REGRESSION_DATASET_ARCHIVED",
            as_=REVIEWER,
        )
        g = (await ev.ok("GET", f"{BASE}/{denied}"))["regression"]
        assert (g["status"], g["scenario_name"], g["dataset_id"]) == ("CANDIDATE", None, None)
        ds = await ev.ok("GET", f"/api/v1/datasets/{first['dataset']['id']}")
        assert (ds["dataset"]["latest_version"], len(ds["version"]["cases"])) == (1, 1)
