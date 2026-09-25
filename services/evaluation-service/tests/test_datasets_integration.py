"""Datasets against PostgreSQL, over HTTP, every exchange checked against the
evaluation contract (and every call the service makes against the
simulation contract)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agenttwin.hashing import content_hash
from agenttwin_core.auth import Principal, Role
from agenttwin_core.events import validate_envelope
from agenttwin_core.ids import new_id
from eval_testutil import ORG, OTHER_PROJECT, PROJECT, SCENARIOS, Stack, evaluation_stack

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

VIEWER = Principal(org_id=ORG, actor="user:viewer", role=Role.VIEWER, project_ids=(PROJECT,))
OUTSIDER = Principal(org_id=ORG, actor="user:outsider", role=Role.ENGINEER, project_ids=(OTHER_PROJECT,))
BOTH = Principal(org_id=ORG, actor="user:both", role=Role.ENGINEER, project_ids=(PROJECT, OTHER_PROJECT))
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


def case(scenario: str, **over: Any) -> dict[str, Any]:
    return {"scenario": scenario} | over


async def create(
    s: Stack, name: str = "release-suite", *cases: dict[str, Any], **over: Any
) -> dict[str, Any]:
    body = {"project_id": PROJECT, "name": name, "cases": list(cases)} | over
    out: dict[str, Any] = await s.ok("POST", "/api/v1/datasets", body, status=201)
    return out


def names(detail: dict[str, Any]) -> list[str]:
    return [c["scenario"] for c in detail["version"]["cases"]]


def fields(c: dict[str, Any]) -> dict[str, Any]:
    return {k: c.get(k) for k in ("scenario", "tags", "source", "trace_id", "privacy", "note")}


async def test_a_dataset_is_created_read_and_listed() -> None:
    async with evaluation_stack() as s:
        created = await create(
            s,
            "release-suite",
            case("refund-over-limit", tags=["refunds", "critical-path", "refunds"]),
            case("refund-happy-path"),
            case(
                "refund-tool-success-lie", source="production_regression", trace_id=TRACE, privacy="redacted"
            ),
            description="What every release is held to.",
            owner="team:support-ai",
            tags=["release", "gate"],
        )
        ds = created["dataset"]
        assert (ds["name"], ds["project_id"], ds["latest_version"], ds["archived"]) == (
            "release-suite",
            PROJECT,
            1,
            False,
        )
        assert ds["tags"] == ["gate", "release"] and ds["created_by"] == "user:engineer"
        version = created["version"]
        assert (version["version"], version["case_count"], version["note"]) == (1, 3, "created")
        # Cases keep the order they were given; tags are a sorted set.
        assert names(created) == ["refund-over-limit", "refund-happy-path", "refund-tool-success-lie"]
        first, _, third = version["cases"]
        assert first["tags"] == ["critical-path", "refunds"] and first["source"] == "manual"
        assert first["privacy"] == "synthetic" and first["added_by"] == "user:engineer"
        assert (third["source"], third["trace_id"], third["privacy"]) == (
            "production_regression",
            TRACE,
            "redacted",
        )
        assert all(c["last_result"] is None for c in version["cases"])
        assert [v["version"] for v in created["versions"]] == [1]

        assert await s.ok("GET", f"/api/v1/datasets/{ds['id']}") == created
        listed = await s.ok("GET", "/api/v1/datasets", project_id=PROJECT)
        assert [(d["name"], d["case_count"]) for d in listed["items"]] == [("release-suite", 3)]
        assert listed["next_cursor"] is None

        # The scenario names were checked with the simulation service, as the
        # evaluation service acting for the project, page by page.
        calls = s.simulation.calls
        assert len(calls) == 3 and {c["actor"] for c in calls} == {"service:evaluation-service"}
        assert {c["project_id"] for c in calls} == {PROJECT}

        # Announced to the audit log, in the envelope the control plane reads.
        [event] = await s.outbox("audit.recorded.v1")
        env = validate_envelope(event)
        assert (env.organization_id, env.project_id, env.producer) == (ORG, PROJECT, "evaluation-service")
        audit = env.payload
        assert (audit["actor"], audit["action"], audit["resource_type"], audit["resource_id"]) == (
            "user:engineer",
            "dataset.create",
            "dataset",
            ds["id"],
        )
        assert audit["after_hash"] == content_hash([fields(c) for c in version["cases"]])
        assert audit["metadata"] == {"name": "release-suite", "version": 1, "cases": 3}


async def test_cases_are_checked_before_anything_is_stored() -> None:
    async with evaluation_stack() as s:
        body = {
            "project_id": PROJECT,
            "name": "suite",
            "cases": [case("refund-happy-path"), case("no-such-one")],
        }
        err = await s.fails("POST", "/api/v1/datasets", body, status=400, code="SCENARIO_NOT_FOUND")
        assert err["details"] == {"missing": ["no-such-one"]}

        body["cases"] = [case("refund-happy-path", source="production_regression")]
        err = await s.fails("POST", "/api/v1/datasets", body, status=400, code="CASE_NOT_REDACTED")
        assert err["details"] == {"scenario": "refund-happy-path"}

        body["cases"] = [case("refund-happy-path"), case("refund-happy-path", note="again")]
        await s.fails("POST", "/api/v1/datasets", body, status=400, code="INVALID_PARAMETER")
        body["cases"] = [case("refund-happy-path", tags=["Not A Tag"])]
        await s.fails("POST", "/api/v1/datasets", body, status=400, code="INVALID_PARAMETER")
        body["cases"] = [case("refund-happy-path", severity="high")]
        await s.fails("POST", "/api/v1/datasets", body, status=400, code="INVALID_REQUEST")
        body["cases"] = [case("refund-happy-path", trace_id="not-a-trace")]
        await s.fails("POST", "/api/v1/datasets", body, status=400, code="INVALID_REQUEST")
        await s.fails(
            "POST",
            "/api/v1/datasets",
            {"project_id": PROJECT, "name": "Bad Name"},
            status=400,
            code="INVALID_REQUEST",
        )
        await s.fails(
            "POST",
            "/api/v1/datasets",
            {"project_id": "not-a-uuid", "name": "x"},
            status=400,
            code="INVALID_PARAMETER",
        )

        assert (await s.ok("GET", "/api/v1/datasets"))["items"] == []
        assert await s.outbox("audit.recorded.v1") == []

        # An empty dataset needs no scenario check.
        s.simulation.calls.clear()
        empty = await create(s, "empty")
        assert empty["version"]["cases"] == [] and s.simulation.calls == []


async def test_every_change_is_a_new_immutable_version() -> None:
    async with evaluation_stack() as s:
        ds = (await create(s, "suite", case("refund-happy-path"), case("refund-over-limit")))["dataset"]
        path = f"/api/v1/datasets/{ds['id']}"

        v2 = await s.ok("POST", f"{path}/cases", {"cases": [case("refund-rate-limited")]}, status=201)
        assert (v2["dataset"]["latest_version"], v2["version"]["version"]) == (2, 2)
        assert names(v2) == ["refund-happy-path", "refund-over-limit", "refund-rate-limited"]
        assert v2["version"]["note"] == "added or updated 1 case(s)"

        # The same request again changes nothing: no new version.
        again = await s.ok("POST", f"{path}/cases", {"cases": [case("refund-rate-limited")]}, status=200)
        assert again["version"]["version"] == 2 and again["dataset"]["latest_version"] == 2

        # Updating a case keeps its place; unchanged cases keep who added them.
        v3 = await s.ok(
            "POST",
            f"{path}/cases",
            {
                "cases": [
                    case("refund-rate-limited"),
                    case("refund-over-limit", tags=["money"], note="escalates above the limit"),
                ],
                "note": "tagged",
            },
            status=201,
            as_=BOTH,
        )
        assert names(v3) == names(v2) and v3["version"]["note"] == "tagged"
        by_name = {c["scenario"]: c for c in v3["version"]["cases"]}
        assert by_name["refund-over-limit"]["tags"] == ["money"]
        assert by_name["refund-over-limit"]["added_by"] == "user:both"
        assert by_name["refund-happy-path"]["added_by"] == "user:engineer"
        # A case sent again unchanged, next to a changed one, stays as it was.
        [before] = [c for c in v2["version"]["cases"] if c["scenario"] == "refund-rate-limited"]
        assert by_name["refund-rate-limited"] == before

        v4 = await s.ok("DELETE", f"{path}/cases/refund-happy-path", as_=BOTH)
        assert names(v4) == ["refund-over-limit", "refund-rate-limited"] and v4["version"]["version"] == 4
        assert v4["version"]["note"] == "removed refund-happy-path"
        await s.fails("DELETE", f"{path}/cases/refund-happy-path", status=404, code="NOT_FOUND")
        await s.fails("DELETE", f"{path}/cases/Not_A_Name", status=400, code="INVALID_PARAMETER")

        # Every version stays readable as it was.
        v1 = await s.ok("GET", path, version=1)
        assert (
            names(v1) == ["refund-happy-path", "refund-over-limit"] and v1["dataset"]["latest_version"] == 4
        )
        assert [v["version"] for v in v1["versions"]] == [4, 3, 2, 1]
        assert names(await s.ok("GET", path, version=3)) == names(v3)
        await s.fails("GET", path, status=404, code="NOT_FOUND", version=5)
        await s.fails("GET", path, status=400, code="INVALID_PARAMETER", version="two")

        # Each change is audited with the cases before and after it.
        events = [e["payload"] for e in await s.outbox("audit.recorded.v1")]
        assert [e["action"] for e in events] == ["dataset.create"] + ["dataset.update"] * 3
        versions = [v1, v2, v3, v4]
        for event, before, after in zip(events[1:], versions[:-1], versions[1:], strict=True):
            assert event["before_hash"] == content_hash([fields(c) for c in before["version"]["cases"]])
            assert event["after_hash"] == content_hash([fields(c) for c in after["version"]["cases"]])
            assert event["metadata"]["version"] == after["version"]["version"]
        assert events[3]["actor"] == "user:both" and events[3]["reason"] == "removed refund-happy-path"


async def test_a_dataset_holds_at_most_500_cases() -> None:
    async with evaluation_stack() as s:
        many = [f"scenario-{i:03d}" for i in range(501)]
        s.simulation.scenarios[PROJECT] = many
        s.simulation.page_size = 200
        body = {"project_id": PROJECT, "name": "big", "cases": [case(n) for n in many]}
        await s.fails("POST", "/api/v1/datasets", body, status=400, code="INVALID_REQUEST")
        ds = (await create(s, "big", *[case(n) for n in many[:500]]))["dataset"]
        path = f"/api/v1/datasets/{ds['id']}"
        err = await s.fails(
            "POST", f"{path}/cases", {"cases": [case(many[500])]}, status=400, code="TOO_MANY_CASES"
        )
        assert "500" in err["message"]
        # Replacing a case keeps the count, so it is allowed.
        full = await s.ok("POST", f"{path}/cases", {"cases": [case(many[0], note="n")]}, status=201)
        assert full["version"]["case_count"] == 500
        await s.fails("GET", path, status=400, code="INVALID_PARAMETER", version=0)


async def test_names_are_unique_per_project_and_archived_datasets_are_frozen() -> None:
    async with evaluation_stack() as s:
        s.simulation.scenarios[OTHER_PROJECT] = ["refund-happy-path"]
        ds = (await create(s, "suite", case("refund-happy-path")))["dataset"]
        err = await s.fails(
            "POST",
            "/api/v1/datasets",
            {"project_id": PROJECT, "name": "suite"},
            status=409,
            code="DATASET_EXISTS",
        )
        assert "suite" in err["message"]
        other = await s.ok(
            "POST",
            "/api/v1/datasets",
            {"project_id": OTHER_PROJECT, "name": "suite", "cases": [case("refund-happy-path")]},
            status=201,
            as_=BOTH,
        )
        assert other["dataset"]["project_id"] == OTHER_PROJECT

        path = f"/api/v1/datasets/{ds['id']}"
        archived = await s.ok("POST", f"{path}/archive")
        assert archived["dataset"]["archived"] is True and archived["dataset"]["latest_version"] == 1
        assert (await s.ok("POST", f"{path}/archive"))["dataset"]["archived"] is True

        listed = await s.ok("GET", "/api/v1/datasets", as_=BOTH)
        assert [(d["name"], d["project_id"]) for d in listed["items"]] == [("suite", OTHER_PROJECT)]
        listed = await s.ok("GET", "/api/v1/datasets", project_id=PROJECT, include_archived="true")
        assert [d["archived"] for d in listed["items"]] == [True]

        await s.fails(
            "POST",
            f"{path}/cases",
            {"cases": [case("refund-over-limit")]},
            status=409,
            code="DATASET_ARCHIVED",
        )
        await s.fails("DELETE", f"{path}/cases/refund-happy-path", status=409, code="DATASET_ARCHIVED")
        assert names(await s.ok("GET", path)) == ["refund-happy-path"]

        # Archiving is audited once, however often it is asked for.
        actions = [e["payload"]["action"] for e in await s.outbox("audit.recorded.v1")]
        assert actions == ["dataset.create", "dataset.create", "dataset.archive"]


async def test_datasets_of_other_projects_look_missing() -> None:
    async with evaluation_stack() as s:
        ds = (await create(s, "suite", case("refund-happy-path")))["dataset"]
        path = f"/api/v1/datasets/{ds['id']}"
        await s.fails("GET", path, status=404, code="NOT_FOUND", as_=OUTSIDER)
        await s.fails(
            "POST", f"{path}/cases", {"cases": [case("x")]}, status=404, code="NOT_FOUND", as_=OUTSIDER
        )
        await s.fails("POST", f"{path}/archive", status=404, code="NOT_FOUND", as_=OUTSIDER)
        await s.fails("DELETE", f"{path}/cases/refund-happy-path", status=404, code="NOT_FOUND", as_=OUTSIDER)
        await s.fails(
            "GET", "/api/v1/datasets", status=404, code="NOT_FOUND", as_=OUTSIDER, project_id=PROJECT
        )
        assert (await s.ok("GET", "/api/v1/datasets", as_=OUTSIDER))["items"] == []
        await s.fails(
            "POST",
            "/api/v1/datasets",
            {"project_id": PROJECT, "name": "mine"},
            status=404,
            code="NOT_FOUND",
            as_=OUTSIDER,
        )
        another_org = Principal(org_id=new_id(), actor="user:x", role=Role.OWNER, all_projects=True)
        await s.fails("GET", path, status=404, code="NOT_FOUND", as_=another_org)
        assert (await s.ok("GET", "/api/v1/datasets", as_=another_org))["items"] == []

        # A viewer reads but does not change.
        assert (await s.ok("GET", path, as_=VIEWER))["dataset"]["id"] == ds["id"]
        await s.fails("POST", f"{path}/archive", status=403, code="FORBIDDEN", as_=VIEWER)
        await s.fails(
            "POST", f"{path}/cases", {"cases": [case("x")]}, status=403, code="FORBIDDEN", as_=VIEWER
        )
        await s.fails(
            "POST",
            "/api/v1/datasets",
            {"project_id": PROJECT, "name": "v"},
            status=403,
            code="FORBIDDEN",
            as_=VIEWER,
        )
        await s.fails("GET", "/api/v1/datasets/not-a-uuid", status=400, code="INVALID_PARAMETER")
        await s.fails("GET", f"/api/v1/datasets/{new_id()}", status=404, code="NOT_FOUND")


async def test_an_unreachable_simulation_service_is_a_503() -> None:
    async with evaluation_stack() as s:
        body = {"project_id": PROJECT, "name": "suite", "cases": [case("refund-happy-path")]}
        for failure in ("down", 500, 503):
            s.simulation.fail = failure  # type: ignore[assignment]
            await s.fails("POST", "/api/v1/datasets", body, status=503, code="UNAVAILABLE")
        assert (await s.ok("GET", "/api/v1/datasets"))["items"] == []
        s.simulation.fail = None
        ds = (await create(s, "suite", case("refund-happy-path")))["dataset"]
        s.simulation.fail = "down"
        await s.fails(
            "POST",
            f"/api/v1/datasets/{ds['id']}/cases",
            {"cases": [case("refund-over-limit")]},
            status=503,
            code="UNAVAILABLE",
        )
        assert (await s.ok("GET", f"/api/v1/datasets/{ds['id']}"))["dataset"]["latest_version"] == 1


async def test_lists_page_and_search() -> None:
    async with evaluation_stack() as s:
        for name in ("e-suite", "a-suite", "c-suite", "b-suite", "d-suite"):
            await create(s, name, description=f"The {name[0].upper()} team's suite.")
        seen: list[str] = []
        cursor = None
        for _ in range(5):
            page = await s.ok("GET", "/api/v1/datasets", limit=2, cursor=cursor)
            seen += [d["name"] for d in page["items"]]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert seen == ["a-suite", "b-suite", "c-suite", "d-suite", "e-suite"]
        found = await s.ok("GET", "/api/v1/datasets", q="C TEAM")
        assert [d["name"] for d in found["items"]] == ["c-suite"]
        assert [d["name"] for d in (await s.ok("GET", "/api/v1/datasets", q="d-su"))["items"]] == ["d-suite"]
        # LIKE wildcards in the query are literal.
        assert (await s.ok("GET", "/api/v1/datasets", q="%"))["items"] == []
        await s.fails("GET", "/api/v1/datasets", status=400, code="INVALID_CURSOR", cursor="!!")
        await s.fails("GET", "/api/v1/datasets", status=400, code="INVALID_PARAMETER", limit=0)
        await s.fails("GET", "/api/v1/datasets", status=400, code="INVALID_PARAMETER", project_id="x")


async def test_concurrent_changes_are_applied_one_after_the_other() -> None:
    async with evaluation_stack() as s:
        ds = (await create(s, "suite"))["dataset"]
        path = f"/api/v1/datasets/{ds['id']}/cases"
        answers = await asyncio.gather(*(s.call("POST", path, {"cases": [case(name)]}) for name in SCENARIOS))
        assert [r.status_code for r in answers] == [201] * len(SCENARIOS)
        latest = await s.ok("GET", f"/api/v1/datasets/{ds['id']}")
        # Every change is in the latest version, each in a version of its own:
        # none was refused, lost or applied twice.
        assert sorted(names(latest)) == sorted(SCENARIOS)
        assert [v["version"] for v in latest["versions"]] == list(range(len(SCENARIOS) + 1, 0, -1))
        assert sorted(r.json()["version"]["version"] for r in answers) == list(range(2, len(SCENARIOS) + 2))
        grown = [len(v["cases"]) for v in [r.json()["version"] for r in answers]]
        assert sorted(grown) == list(range(1, len(SCENARIOS) + 1))


async def test_a_case_shows_its_latest_completed_result() -> None:
    async with evaluation_stack() as s:
        ds = (await create(s, "suite", case("refund-happy-path"), case("refund-over-limit")))["dataset"]

        async def eval_run(status: str, finished: str, classification: str, candidate: str) -> str:
            # Evaluation runs are written by the worker; the query only reads them.
            run_id = new_id()
            await s.store.all(
                """INSERT INTO eval_run (id, organization_id, project_id, agent_name, baseline_version,
                       candidate_version, dataset_id, dataset_version, selection, status, requested_by,
                       finished_at)
                   VALUES (%s, %s, %s, 'support-refund-agent', '1.2.4', %s, %s, 1, '{}', %s, 'user:e', %s)
                   RETURNING id""",
                (run_id, ORG, PROJECT, candidate, ds["id"], status, finished),
            )
            await s.store.all(
                """INSERT INTO eval_case_result (eval_run_id, position, scenario_name, severity,
                       classification, sides, comparison)
                   VALUES (%s, 0, 'refund-over-limit', 'critical', %s, '{}',
                           '{"baseline": {"status": "PASSED"}, "candidate": {"status": "FAILED"}}')
                   RETURNING eval_run_id""",
                (run_id, classification),
            )
            return run_id

        older = await eval_run("COMPLETED", "2026-09-01T10:00:00Z", "UNCHANGED", "1.3.0")
        latest = await eval_run("COMPLETED", "2026-09-02T10:00:00Z", "NEW_CRITICAL_FAILURE", "1.3.1")
        await eval_run("FAILED", "2026-09-03T10:00:00Z", "IMPROVED", "1.3.2")

        detail = await s.ok("GET", f"/api/v1/datasets/{ds['id']}")
        by_name = {c["scenario"]: c["last_result"] for c in detail["version"]["cases"]}
        assert by_name["refund-happy-path"] is None
        result = by_name["refund-over-limit"]
        assert result["eval_run_id"] == latest != older
        assert (result["classification"], result["candidate_version"]) == ("NEW_CRITICAL_FAILURE", "1.3.1")
        assert (result["baseline_status"], result["candidate_status"]) == ("PASSED", "FAILED")
        assert result["finished_at"] == "2026-09-02T10:00:00Z"
