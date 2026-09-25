"""The simulation contract as a document (ADR-0021): a valid OpenAPI 3.1
document whose operations are exactly the service's routes and which follows
the API conventions. Whether the service answers as documented is checked by
the integration tests, which run every exchange through the contract."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi.routing import APIRoute
from openapi_spec_validator import validate
from openapi_spec_validator.readers import read_from_filename

from agenttwin_core.evaluators import default_registry
from agenttwin_core.logx import get_logger
from agenttwin_core.openapi_contract import Contract, ContractViolation, contract_path
from agenttwin_core.web import Health, build_app
from agenttwin_simulation.api import SimulationAPI, twin_json
from agenttwin_simulation.clients import AgentCall, ControlPlaneClient
from agenttwin_simulation.config import SimulationConfig
from agenttwin_simulation.runner import agent_result_json
from agenttwin_simulation.store import Store
from agenttwin_simulation.twin_http import TwinEndpoint

PATH = contract_path("simulation-service")
CONTRACT = Contract.load(PATH)
DOC: dict[str, Any] = dict(CONTRACT.document)
# Infrastructure routes every service has; they are not part of an API contract.
INFRASTRUCTURE = {"/health/live", "/health/ready", "/metrics"}


def service_routes() -> set[tuple[str, str]]:
    cfg = SimulationConfig(
        control_plane_url="http://control-plane.test",
        trace_service_url="http://trace-service.test",
        twin_public_url="http://simulation-service.test/twin/v1",
    )
    app = build_app(
        service="simulation-service",
        version="test",
        health=Health(),
        tokens=None,
        audience="simulation-service",
    )
    # Registering routes touches none of the dependencies.
    SimulationAPI(
        store=cast(Store, None),
        cfg=cfg,
        registry=default_registry(),
        control_plane=cast(ControlPlaneClient, None),
        log=get_logger("test"),
    ).routes(app)
    TwinEndpoint(cast(Store, None), cfg).routes(app)
    return {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute) and route.path not in INFRASTRUCTURE
        for method in route.methods
    }


def operations(public: bool | None = None) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for path, item in DOC["paths"].items():
        if public is not None and path.startswith("/api/") != public:
            continue
        for method, op in item.items():
            if method in ("get", "post", "put", "patch", "delete"):
                yield method.upper(), path, op


def refs(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str):
            yield node["$ref"]
        for value in node.values():
            yield from refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from refs(value)


def test_the_document_is_valid_openapi_31() -> None:
    spec, base = read_from_filename(str(PATH))
    validate(spec, base_uri=base)
    assert DOC["openapi"] == "3.1.0"


def test_operations_are_exactly_the_service_routes() -> None:
    documented = {(m, p) for m, p, _ in operations()}
    served = service_routes()
    assert served - documented == set(), "routes the contract does not document"
    assert documented - served == set(), "documented operations the service does not serve"
    assert len(served) == 16


def test_every_operation_follows_the_conventions() -> None:
    ids = []
    for method, path, op in operations():
        where = f"{method} {path}"
        ids.append(op["operationId"])
        assert op.get("summary") and op.get("tags"), where
        responses = op["responses"]
        params = [CONTRACT.follow(p) for p in op.get("parameters", [])]
        declared = {p["name"] for p in params if p["in"] == "path"}
        assert declared == set(re.findall(r"\{(\w+)\}", path)), where
        if not path.startswith("/api/"):
            assert op["security"] == [{"caseToken": []}], where
            continue
        assert "security" not in op, f"{where}: the public API uses the edge's credentials"
        assert {"401", "429", "500", "502", "503"} <= set(responses), where
        if op.get("requestBody") or any(p["in"] in ("path", "query") for p in params):
            assert "400" in responses, where
        if declared:
            assert "404" in responses, where
        if method == "POST":
            refs_ = [p["$ref"] for p in op["parameters"] if "$ref" in p]
            assert "#/components/parameters/IdempotencyKey" in refs_, where
            assert {"409", "422"} <= set(responses), where
        body = op.get("requestBody")
        if body is not None:
            schema = CONTRACT.follow(body["content"]["application/json"]["schema"])
            # The service rejects unknown fields; the contract says so.
            assert schema.get("additionalProperties") is False, where
        for status, response in responses.items():
            if status.startswith("2"):
                assert "application/json" in CONTRACT.follow(response)["content"], where
    assert len(ids) == len(set(ids))


def test_every_component_is_used() -> None:
    used = set(refs({k: v for k, v in DOC.items() if k != "components"}))
    # Components referenced only by other components count once reached.
    frontier = list(used)
    while frontier:
        target = CONTRACT.pointer(frontier.pop())
        for ref in refs(target):
            if ref not in used:
                used.add(ref)
                frontier.append(ref)
    for kind, items in DOC["components"].items():
        if kind == "securitySchemes":
            continue
        for name in items:
            assert f"#/components/{kind}/{name}" in used, f"unused component {kind}/{name}"


# ---------------------------------------------------------------- the documented shape


def call(body: Any, kind: Any = "ok", status: int | None = 200) -> AgentCall:
    return AgentCall(kind, status, body, None if kind == "ok" else "boom", 12.5)


def test_agent_answers_are_kept_in_the_documented_shape() -> None:
    good = {
        "output": "Your refund of $40 is on its way.",
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "status": "completed",
        "agent_version": "1.2.4",
        "model": "scripted-planner-v1",
        "model_kind": "deterministic-fake",
        "steps": 4,
        "claimed_outcome": "refund_issued",
        "business_outcome": None,
        "tool_calls": [{"tool": "lookup_order"}],
        "extra": "ignored",
    }
    kept = agent_result_json(call(good))
    CONTRACT.check_schema("AgentResult", kept)
    assert kept["steps"] == 4 and kept["business_outcome"] is None and "extra" not in kept

    # An agent that ignores the adapter contract: values of other types are
    # dropped rather than served against the documented types.
    odd = {
        "output": {"text": "hi"},
        "steps": "4",
        "business_outcome": 7,
        "status": True,
        "tool_calls": ["lookup_order", {"tool": "refund_payment"}],
    }
    kept = agent_result_json(call(odd))
    CONTRACT.check_schema("AgentResult", kept)
    assert kept == {
        "kind": "ok",
        "http_status": 200,
        "elapsed_ms": 12.5,
        "tool_calls": [{"tool": "refund_payment"}],
    }
    failed = agent_result_json(call({"error": {"code": "BOOM", "message": "x" * 900}}, "http_error", 500))
    CONTRACT.check_schema("AgentResult", failed)
    assert failed["agent_error"] == {"code": "BOOM", "message": "x" * 500}


def test_a_twin_carries_its_description_everywhere() -> None:
    row = {
        "id": "0190f3b4-0000-7000-8000-000000000001",
        "organization_id": "0190f3b4-0000-7000-8000-00000000000a",
        "project_id": "0190f3b4-0000-7000-8000-0000000000b1",
        "name": "demo-co-support",
        "version": 1,
        "spec_hash": "0" * 64,
        "tool_count": 3,
        "created_by": "user:engineer",
        "created_at": "2026-09-25T10:00:00Z",
    }
    listed = twin_json(row | {"description": "Listed"})
    full = twin_json(row | {"document": {"metadata": {"name": "demo-co-support", "description": "Full"}}})
    bare = twin_json(row | {"document": {"metadata": {"name": "demo-co-support"}}})
    for twin in (listed, full, bare):
        CONTRACT.check_schema("TwinSummary", twin)
    assert (listed["description"], full["description"], bare["description"]) == ("Listed", "Full", None)


@pytest.mark.parametrize(
    ("name", "instance", "problem"),
    [
        ("TwinSummary", {"id": "x"}, "is a required property"),
        ("QueuedCase", {"id": "0190f3b4-0000-7000-8000-000000000001"}, "is a required property"),
        ("Error", {"error": {"code": "NOT_FOUND", "message": "m", "request_id": "r", "extra": 1}}, "extra"),
    ],
)
def test_check_schema_reports_problems(name: str, instance: Any, problem: str) -> None:
    with pytest.raises(ContractViolation, match=problem):
        CONTRACT.check_schema(name, instance)
