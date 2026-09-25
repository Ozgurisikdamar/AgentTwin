"""Every operation of the simulation contract, exercised on a real stack
(ADR-0021). The stack checks each exchange against
``packages/contracts/openapi/simulation-service.openapi.yaml`` — responses
strictly, accepted requests, and the worker's calls to the agent — so this
test makes sure no operation is left unchecked, and pins the documented
answers that are easy to break without noticing."""

from __future__ import annotations

import uuid

import pytest

from agenttwin_core.auth import Principal, Role, service_principal
from sim_testutil import AGENT, ORG, PROJECT, running_cases, scenario_yaml, simulation_stack, twin_yaml

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

HAPPY = "refund-happy-path"
VIEWER = Principal(org_id=ORG, actor="user:viewer", role=Role.VIEWER, project_ids=(PROJECT,))
EVALUATION = service_principal("evaluation-service", ORG, PROJECT)


async def test_every_operation_is_exercised_and_answers_as_documented() -> None:
    async with simulation_stack() as s:
        # -- twins ------------------------------------------------------------------
        twin_doc = {"project_id": PROJECT, "yaml": twin_yaml()}
        registered = await s.ok("POST", "/api/v1/twins", twin_doc, status=201)
        twin = registered["twin"]
        assert registered["created"] is True and twin["description"]
        again = await s.ok("POST", "/api/v1/twins", twin_doc)
        assert (again["created"], again["twin"]["id"]) == (False, twin["id"])
        listed = await s.ok("GET", "/api/v1/twins", project_id=PROJECT)
        assert [(t["id"], t["description"]) for t in listed["items"]] == [(twin["id"], twin["description"])]
        detail = await s.ok("GET", f"/api/v1/twins/{twin['id']}")
        assert detail["twin"]["description"] == twin["description"]
        assert [v["version"] for v in detail["versions"]] == [1]

        # -- scenarios --------------------------------------------------------------
        doc = {"project_id": PROJECT, "yaml": scenario_yaml(HAPPY)}
        check = await s.ok("POST", "/api/v1/scenarios/validate", doc)
        assert check["valid"] is True and check["twin"]["id"] == twin["id"]
        broken = await s.ok("POST", "/api/v1/scenarios/validate", {"project_id": PROJECT, "yaml": "spec: [1"})
        assert (broken["valid"], broken["spec_hash"], broken["twin"]) == (False, None, None)
        saved = await s.ok("POST", "/api/v1/scenarios", doc, status=201)
        scenario_id = saved["scenario"]["id"]
        page = await s.ok("GET", "/api/v1/scenarios", project_id=PROJECT, name=HAPPY, limit=10)
        assert [it["id"] for it in page["items"]] == [scenario_id] and page["next_cursor"] is None
        version = await s.ok("GET", f"/api/v1/scenarios/{scenario_id}", version=1)
        assert (version["version"], version["scenario"]["spec_hash"]) == (1, saved["scenario"]["spec_hash"])

        # -- runs: one through the worker, which calls the agent -------------------
        caps = await s.ok("GET", "/api/v1/simulations/capabilities")
        assert caps["agents"] == [AGENT] and "timeout_after_mutation" in caps["fault_types"]
        run_id = await s.start_run("1.2.4", HAPPY)
        assert await s.worker.process_next() == run_id
        # (``status`` is a query parameter here, so the raw call is used.)
        runs = await s.call("GET", "/api/v1/simulations", project_id=PROJECT, agent=AGENT, status="COMPLETED")
        assert runs.status_code == 200 and [r["id"] for r in runs.json()["items"]] == [run_id]
        run = await s.ok("GET", f"/api/v1/simulations/{run_id}")
        case_id = run["cases"][0]["id"]
        case = await s.ok("GET", f"/api/v1/simulations/{run_id}/cases/{case_id}")
        assert case["case"]["verdict"]["status"] == "PASSED"
        assert case["case"]["agent_result"]["kind"] == "ok"
        assert {st["kind"] for st in case["steps"]} >= {"tool_call"}
        # Cancelling a final run changes nothing and says so with 200.
        final = await s.ok("POST", f"/api/v1/simulations/{run_id}/cancel")
        assert (final["run"]["status"], final["run"]["cancel_requested"]) == ("COMPLETED", False)

        # -- the twin runtime, called with a case token as an agent would ----------
        second = await s.start_run("1.2.4", HAPPY)
        await running_cases(s, second, {HAPPY: "tok-contract"})
        auth = {"Authorization": "Bearer tok-contract"}
        reply = await s.client.post(
            "/twin/v1/tools/lookup_order", json={"order_id": "ORD-1001"}, headers=auth
        )
        assert reply.status_code == 200
        kb = await s.client.get("/twin/v1/kb/search", params={"q": "refund", "limit": 2}, headers=auth)
        assert kb.status_code == 200 and kb.json()["documents"]
        stopping = await s.ok("POST", f"/api/v1/simulations/{second}/cancel", status=202)
        assert stopping["run"]["cancel_requested"] is True

        # -- the evaluation service's pair of runs ------------------------------------
        eval_run = str(uuid.uuid4())
        pair_body = {
            "project_id": PROJECT,
            "eval_run_id": eval_run,
            "agent": AGENT,
            "baseline_version": "1.2.4",
            "candidate_version": "1.3.0",
            "scenarios": [HAPPY],
        }
        created = await s.call("POST", "/internal/v1/simulation-pairs", pair_body, as_=EVALUATION)
        assert created.status_code == 201 and created.json()["created"] is True
        replayed = await s.call("POST", "/internal/v1/simulation-pairs", pair_body, as_=EVALUATION)
        assert replayed.status_code == 200 and replayed.json()["created"] is False
        conflict = await s.call(
            "POST", "/internal/v1/simulation-pairs", pair_body | {"seed": 1}, as_=EVALUATION
        )

        archived = await s.ok("POST", f"/api/v1/scenarios/{scenario_id}/archive")
        assert archived["scenario"]["archived"] is True

        # -- documented refusals ------------------------------------------------------
        refusals = [
            (await s.call("GET", "/api/v1/twins/not-a-uuid"), 400, "INVALID_PARAMETER"),
            (await s.call("GET", f"/api/v1/scenarios/{uuid.uuid4()}"), 404, "NOT_FOUND"),
            (await s.call("GET", "/api/v1/scenarios", limit=0), 400, "INVALID_PARAMETER"),
            (
                await s.call("POST", "/api/v1/simulations", {"project_id": PROJECT, "agent": AGENT, "x": 1}),
                400,
                "INVALID_REQUEST",
            ),
            (await s.call("POST", "/api/v1/twins", twin_doc, as_=VIEWER), 403, "FORBIDDEN"),
            (await s.client.get("/api/v1/simulations"), 401, "UNAUTHENTICATED"),
            (
                await s.client.post(
                    "/api/v1/scenarios",
                    content=b"project_id=x",
                    headers={
                        "Authorization": "Bearer " + s.token(),
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                ),
                415,
                "UNSUPPORTED_MEDIA_TYPE",
            ),
            (
                await s.client.get("/twin/v1/kb/search", params={"q": "x", "limit": 99}, headers=auth),
                400,
                "INVALID_PARAMETER",
            ),
            (await s.client.get("/twin/v1/kb/search", params={"q": "x"}), 401, "TWIN_CREDENTIAL_REQUIRED"),
            (
                await s.client.post("/twin/v1/tools/lookup_order", json={}, headers=auth),
                409,
                "RUN_CANCELLED",
            ),
            (conflict, 409, "PAIR_CONFLICT"),
            (await s.call("POST", "/internal/v1/simulation-pairs", pair_body), 403, "FORBIDDEN"),
        ]
        assert [(r.status_code, r.json()["error"]["code"]) for r, _, _ in refusals] == [
            (status, code) for _, status, code in refusals
        ]
        for r, _, _ in refusals:
            assert r.json()["error"]["request_id"] == r.headers["x-request-id"]

        assert s.contract.uncovered() == [], "operations without a checked success response"
