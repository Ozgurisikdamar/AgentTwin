"""The simulation API on a real PostgreSQL: document validation, versioning,
pagination, run requests and access control (other projects answer 404,
missing permissions 403)."""

from __future__ import annotations

import re
from typing import Any

import pytest

from agenttwin_core.auth import Principal, Role
from sim_testutil import AGENT, ORG, OTHER_PROJECT, PROJECT, Stack, scenario_yaml, simulation_stack, twin_yaml

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

HAPPY = "refund-happy-path"
CANARY = "sk-demo-internal-9f8e7d6c5b4a39281706"
VIEWER = Principal(org_id=ORG, actor="user:viewer", role=Role.VIEWER, project_ids=(PROJECT,))
OUTSIDER = Principal(org_id=ORG, actor="user:outsider", role=Role.ENGINEER, project_ids=(OTHER_PROJECT,))


def scenario(
    name: str, *, severity: str = "medium", tags: tuple[str, ...] = (), **spec: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "agent": AGENT,
        "twin": "demo-co-support",
        "input": {
            "message": "Where is my order ORD-1001?",
            "context": {"tenant": "demo-co", "customer_id": "CUS-100"},
        },
        "expectations": [{"type": "toolCalled", "tool": "lookup_order"}],
    }
    body.update(spec)
    return {
        "apiVersion": "agenttwin.dev/v1",
        "kind": "Scenario",
        "metadata": {"name": name, "severity": severity, "tags": list(tags)},
        "spec": body,
    }


async def error(
    s: Stack, method: str, path: str, body: Any = None, *, as_: Principal | None = None, **params: Any
) -> tuple[int, str, dict[str, Any]]:
    r = await s.call(method, path, body, as_=as_, **params)
    err = r.json()["error"]
    return r.status_code, err["code"], err.get("details") or {}


async def test_documents_validation_and_versioning() -> None:
    async with simulation_stack() as s:
        # A scenario is checked against the twin it names, which must exist.
        check = await s.ok(
            "POST", "/api/v1/scenarios/validate", {"project_id": PROJECT, "yaml": scenario_yaml(HAPPY)}
        )
        assert check["valid"] is False and check["twin"] is None
        assert check["problems"] == [
            "spec.twin: there is no twin named 'demo-co-support' in this project "
            "(register it first: POST /api/v1/twins)"
        ]
        code = await error(
            s, "POST", "/api/v1/scenarios", {"project_id": PROJECT, "yaml": scenario_yaml(HAPPY)}
        )
        assert code[:2] == (400, "SCENARIO_INVALID")

        # Twins: registered, idempotent, versioned by content.
        first = await s.ok("POST", "/api/v1/twins", {"project_id": PROJECT, "yaml": twin_yaml()}, status=201)
        twin = first["twin"]
        assert (twin["name"], twin["version"], first["created"]) == ("demo-co-support", 1, True)
        tools = {t["name"]: t for t in twin["tools"]}
        assert tools["refund_payment"]["risk"] == "WRITE_IRREVERSIBLE"
        assert tools["refund_payment"]["idempotency"] == "idempotency_key"
        assert tools["export_customer_data"]["risk"] == "ADMIN"
        again = await s.ok("POST", "/api/v1/twins", {"project_id": PROJECT, "yaml": twin_yaml()})
        assert (again["created"], again["twin"]["id"]) == (False, twin["id"])
        changed = twin_yaml().replace("max_auto_refund: 100", "max_auto_refund: 150")
        second = await s.ok("POST", "/api/v1/twins", {"project_id": PROJECT, "yaml": changed}, status=201)
        assert second["twin"]["version"] == 2 and second["twin"]["spec_hash"] != twin["spec_hash"]

        # The planted canary is never shown back, in the document or its YAML.
        r = await s.call("GET", f"/api/v1/twins/{twin['id']}")
        assert r.status_code == 200 and CANARY not in r.text
        detail = r.json()
        assert detail["document"]["spec"]["secrets"] == ["<redacted canary: 37 characters>"]
        assert [v["version"] for v in detail["versions"]] == [2, 1]  # newest first
        assert detail["twin"]["tenant_key"] == "tenant"
        listed = await s.ok("GET", "/api/v1/twins", project_id=PROJECT)
        assert [(t["name"], t["version"]) for t in listed["items"]] == [("demo-co-support", 2)]

        bad = await error(
            s,
            "POST",
            "/api/v1/twins",
            {
                "project_id": PROJECT,
                "yaml": "apiVersion: agenttwin.dev/v1\nkind: TwinDefinition\nmetadata: {}",
            },
        )
        assert bad[:2] == (400, "TWIN_INVALID") and bad[2]["problems"]

        # Scenarios: validated against the latest twin, cross-checked tools.
        ghost = scenario("ghost-tool", allowedTools=["lookup_order", "wire_money"])
        check = await s.ok("POST", "/api/v1/scenarios/validate", {"project_id": PROJECT, "document": ghost})
        assert check["valid"] is False
        assert any("wire_money" in problem for problem in check["problems"]), check["problems"]
        check = await s.ok(
            "POST", "/api/v1/scenarios/validate", {"project_id": PROJECT, "yaml": scenario_yaml(HAPPY)}
        )
        assert check["valid"] is True and check["twin"]["version"] == 2
        assert re.fullmatch(r"[0-9a-f]{64}", check["spec_hash"])
        broken = await s.ok("POST", "/api/v1/scenarios/validate", {"project_id": PROJECT, "yaml": "a: [1,"})
        assert broken["valid"] is False and broken["spec_hash"] is None and broken["problems"]
        # Viewers may validate (read-only) but not save.
        r = await s.call(
            "POST",
            "/api/v1/scenarios/validate",
            {"project_id": PROJECT, "yaml": scenario_yaml(HAPPY)},
            as_=VIEWER,
        )
        assert r.status_code == 200
        assert (
            await error(
                s, "POST", "/api/v1/scenarios", {"project_id": PROJECT, "document": scenario("x")}, as_=VIEWER
            )
        )[:2] == (403, "FORBIDDEN")

        created = await s.ok(
            "POST", "/api/v1/scenarios", {"project_id": PROJECT, "yaml": scenario_yaml(HAPPY)}, status=201
        )
        sc = created["scenario"]
        assert (created["version"], created["created"], sc["severity"]) == (1, True, "high")
        same = await s.ok("POST", "/api/v1/scenarios", {"project_id": PROJECT, "yaml": scenario_yaml(HAPPY)})
        assert (same["created"], same["version"]) == (False, 1)
        edited = scenario_yaml(HAPPY).replace("severity: high", "severity: critical")
        v2 = await s.ok("POST", "/api/v1/scenarios", {"project_id": PROJECT, "yaml": edited}, status=201)
        assert (v2["version"], v2["scenario"]["id"], v2["scenario"]["severity"]) == (2, sc["id"], "critical")
        events = await s.outbox("scenario.upserted.v1")
        assert [e["payload"]["version"] for e in events] == [1, 2]
        assert events[0]["payload"] == events[0]["payload"] | {
            "scenario_id": sc["id"],
            "name": HAPPY,
            "agent": AGENT,
            "twin": "demo-co-support",
        }

        latest = await s.ok("GET", f"/api/v1/scenarios/{sc['id']}")
        assert latest["version"] == 2 and latest["document"]["metadata"]["severity"] == "critical"
        assert [v["version"] for v in latest["versions"]] == [2, 1]
        assert "severity: critical" in latest["yaml"]
        old = await s.ok("GET", f"/api/v1/scenarios/{sc['id']}", version=1)
        assert old["version"] == 1 and old["document"]["metadata"]["severity"] == "high"
        assert (await error(s, "GET", f"/api/v1/scenarios/{sc['id']}", version=3))[:2] == (404, "NOT_FOUND")
        assert (await error(s, "GET", f"/api/v1/scenarios/{sc['id']}", version="x"))[:2] == (
            400,
            "INVALID_PARAMETER",
        )
        assert (await error(s, "GET", "/api/v1/scenarios/not-a-uuid"))[:2] == (400, "INVALID_PARAMETER")
        unknown = await error(s, "POST", "/api/v1/scenarios", {"project_id": PROJECT, "document": {}, "x": 1})
        assert unknown[:2] == (400, "INVALID_REQUEST")


async def test_scenario_listing_filters_pagination_and_archive() -> None:
    async with simulation_stack() as s:
        await s.register_demo()
        names = [f"order-status-{i}" for i in range(5)]
        for i, name in enumerate(names):
            doc = scenario(name, severity="low" if i % 2 else "medium", tags=("smoke",) if i < 2 else ())
            await s.ok("POST", "/api/v1/scenarios", {"project_id": PROJECT, "document": doc}, status=201)

        pages: list[list[str]] = []
        cursor = None
        while True:
            page = await s.ok("GET", "/api/v1/scenarios", project_id=PROJECT, limit=2, cursor=cursor)
            pages.append([it["name"] for it in page["items"]])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert pages == [names[0:2], names[2:4], names[4:5]]

        low = await s.ok("GET", "/api/v1/scenarios", project_id=PROJECT, severity="low")
        assert [it["name"] for it in low["items"]] == [names[1], names[3]]
        smoke = await s.ok("GET", "/api/v1/scenarios", project_id=PROJECT, tag="smoke")
        assert [it["name"] for it in smoke["items"]] == names[:2]
        search = await s.ok("GET", "/api/v1/scenarios", project_id=PROJECT, q="status-3")
        assert [it["name"] for it in search["items"]] == [names[3]]
        # name= is exact, unlike the q= substring search.
        exact = await s.ok("GET", "/api/v1/scenarios", project_id=PROJECT, name=names[3])
        assert [it["name"] for it in exact["items"]] == [names[3]]
        assert (await s.ok("GET", "/api/v1/scenarios", project_id=PROJECT, name="status-3"))["items"] == []
        assert (await error(s, "GET", "/api/v1/scenarios", severity="urgent"))[:2] == (
            400,
            "INVALID_PARAMETER",
        )
        assert (await error(s, "GET", "/api/v1/scenarios", cursor="%%%"))[:2] == (400, "INVALID_CURSOR")
        assert (await error(s, "GET", "/api/v1/scenarios", limit=0))[:2] == (400, "INVALID_PARAMETER")

        # Archived scenarios drop out of listings and runs.
        target = next(
            it for it in (await s.ok("GET", "/api/v1/scenarios"))["items"] if it["name"] == names[0]
        )
        archived = await s.ok("POST", f"/api/v1/scenarios/{target['id']}/archive")
        assert archived["scenario"]["archived"] is True
        visible = await s.ok("GET", "/api/v1/scenarios", project_id=PROJECT)
        assert names[0] not in [it["name"] for it in visible["items"]]
        everything = await s.ok("GET", "/api/v1/scenarios", project_id=PROJECT, include_archived="true")
        assert names[0] in [it["name"] for it in everything["items"]]
        missing = await error(
            s,
            "POST",
            "/api/v1/simulations",
            {
                "project_id": PROJECT,
                "agent": AGENT,
                "agent_version": "1.2.4",
                "scenarios": [names[0], names[1]],
            },
        )
        assert missing == (400, "SCENARIO_NOT_FOUND", {"missing": [names[0]]})
        by_tag = await s.ok(
            "POST",
            "/api/v1/simulations",
            {"project_id": PROJECT, "agent": AGENT, "agent_version": "1.2.4", "tags": ["smoke"]},
            status=202,
        )
        assert [c["scenario_name"] for c in by_tag["cases"]] == [names[1]]


async def test_run_requests_are_checked_before_anything_is_queued() -> None:
    async with simulation_stack() as s:
        await s.register_demo(HAPPY)

        def req(**extra: Any) -> dict[str, Any]:
            return {"project_id": PROJECT, "agent": AGENT, "agent_version": "1.2.4"} | extra

        not_found = await error(s, "POST", "/api/v1/simulations", req(agent_version="9.9.9"))
        assert not_found[:2] == (400, "AGENT_VERSION_NOT_FOUND")
        unknown = await error(s, "POST", "/api/v1/simulations", req(scenarios=["no-such-scenario"]))
        assert unknown == (400, "SCENARIO_NOT_FOUND", {"missing": ["no-such-scenario"]})
        none = await error(s, "POST", "/api/v1/simulations", req(tags=["nightly"]))
        assert none[:2] == (400, "NO_SCENARIOS")
        bad_tag = await error(s, "POST", "/api/v1/simulations", req(tags=["Bad Tag"]))
        assert bad_tag[:2] == (400, "INVALID_PARAMETER")
        not_runnable = await error(s, "POST", "/api/v1/simulations", req(agent="another-agent"))
        assert not_runnable == (400, "AGENT_NOT_RUNNABLE", {"runnable": [AGENT]})
        bad_seed = await error(s, "POST", "/api/v1/simulations", req(seed=-1))
        assert bad_seed[:2] == (400, "INVALID_REQUEST")
        # The control plane is down: nothing can be pinned, nothing is queued.
        s.control_plane.fail = 503
        down = await error(s, "POST", "/api/v1/simulations", req())
        assert down[:2] == (503, "UNAVAILABLE")
        s.control_plane.fail = None
        assert await s.outbox("simulation.run_requested.v1") == []
        assert await s.store.all("SELECT id FROM simulation_run") == []

        # Every scenario of the agent runs when none is named.
        run = await s.ok(
            "POST", "/api/v1/simulations", req(release_id="rel-42", side="CANDIDATE"), status=202
        )
        assert run["run"]["release_id"] == "rel-42" and run["run"]["side"] == "CANDIDATE"
        assert [c["scenario_name"] for c in run["cases"]] == [HAPPY]
        # The control plane was asked as the simulation service of this project.
        assert s.control_plane.calls[-1] == {"project_id": PROJECT, "agent": AGENT, "version": "1.2.4"}

        caps = await s.ok("GET", "/api/v1/simulations/capabilities")
        assert caps["agents"] == [AGENT] and caps["engine"] == "twin-engine/1.0.0"
        assert len(caps["fault_types"]) == 20
        assert {"timeout_after_mutation", "success_without_mutation", "dropped_connection"} <= set(
            caps["fault_types"]
        )
        assert {"state", "order", "noDuplicateSideEffect"} <= set(caps["expectation_types"])
        assert caps["limits"]["max_cases"] == 500


async def test_access_is_scoped_to_the_callers_projects() -> None:
    async with simulation_stack() as s:
        await s.register_demo(HAPPY)
        run_id = await s.start_run("1.2.4", HAPPY)
        scenario_id = (await s.ok("GET", "/api/v1/scenarios"))["items"][0]["id"]
        twin_id = (await s.ok("GET", "/api/v1/twins"))["items"][0]["id"]
        case_id = (await s.ok("GET", f"/api/v1/simulations/{run_id}"))["cases"][0]["id"]

        # Another project's engineer: everything looks missing (no probing).
        for path in (
            f"/api/v1/simulations/{run_id}",
            f"/api/v1/simulations/{run_id}/cases/{case_id}",
            f"/api/v1/scenarios/{scenario_id}",
            f"/api/v1/twins/{twin_id}",
        ):
            assert (await error(s, "GET", path, as_=OUTSIDER))[:2] == (404, "NOT_FOUND"), path
        assert (await error(s, "POST", f"/api/v1/simulations/{run_id}/cancel", as_=OUTSIDER))[:2] == (
            404,
            "NOT_FOUND",
        )
        assert (await error(s, "POST", f"/api/v1/scenarios/{scenario_id}/archive", as_=OUTSIDER))[:2] == (
            404,
            "NOT_FOUND",
        )
        assert (await error(s, "GET", "/api/v1/simulations", as_=OUTSIDER, project_id=PROJECT))[:2] == (
            404,
            "NOT_FOUND",
        )
        write = {"project_id": PROJECT, "document": scenario("sneaky")}
        assert (await error(s, "POST", "/api/v1/scenarios", write, as_=OUTSIDER))[:2] == (404, "NOT_FOUND")
        for path in ("/api/v1/simulations", "/api/v1/scenarios", "/api/v1/twins"):
            r = await s.call("GET", path, as_=OUTSIDER)
            assert r.status_code == 200 and r.json()["items"] == [], path

        # A viewer reads but cannot start, cancel or change anything.
        assert (await s.call("GET", f"/api/v1/simulations/{run_id}", as_=VIEWER)).status_code == 200
        start = {"project_id": PROJECT, "agent": AGENT, "agent_version": "1.2.4"}
        for method, path, body in (
            ("POST", "/api/v1/simulations", start),
            ("POST", f"/api/v1/simulations/{run_id}/cancel", None),
            ("POST", f"/api/v1/scenarios/{scenario_id}/archive", None),
            ("POST", "/api/v1/twins", {"project_id": PROJECT, "yaml": twin_yaml()}),
        ):
            assert (await error(s, method, path, body, as_=VIEWER))[:2] == (403, "FORBIDDEN"), path
        # Without a token nothing is served.
        anon = await s.client.get(f"/api/v1/simulations/{run_id}")
        assert anon.status_code == 401

        # Listing runs: filters, validation and pagination.
        second = await s.start_run("1.2.4", HAPPY)
        page = await s.ok("GET", "/api/v1/simulations", limit=1)
        assert [it["id"] for it in page["items"]] == [second] and page["next_cursor"]
        rest = await s.ok("GET", "/api/v1/simulations", limit=1, cursor=page["next_cursor"])
        assert [it["id"] for it in rest["items"]] == [run_id] and rest["next_cursor"] is None
        # (``status`` is a query parameter here, so the raw call is used.)
        queued = await s.call("GET", "/api/v1/simulations", status="QUEUED", agent=AGENT)
        assert [it["id"] for it in queued.json()["items"]] == [second, run_id]
        done = await s.call("GET", "/api/v1/simulations", status="COMPLETED")
        assert (done.status_code, done.json()["items"]) == (200, [])
        assert (await error(s, "GET", "/api/v1/simulations", status="DONE"))[:2] == (400, "INVALID_PARAMETER")
        assert (await error(s, "GET", "/api/v1/simulations", cursor="bogus"))[:2] == (400, "INVALID_CURSOR")
