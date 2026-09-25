"""The evaluation contract as a document (ADR-0021): its operations are
exactly the service's routes. The rules every API document follows are
checked for all of them in ``scripts/tests/test_api_documents.py``; whether
the service answers as documented is checked by the integration tests, which
run every exchange through the contract."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

from fastapi.routing import APIRoute

from agenttwin_core.logx import get_logger
from agenttwin_core.openapi_contract import Contract, contract_path
from agenttwin_core.web import Health, build_app
from agenttwin_evaluation.clients import SimulationClient
from agenttwin_evaluation.datasets import DatasetsAPI
from agenttwin_evaluation.store import Store

CONTRACT = Contract.load(contract_path("evaluation-service"))
DOC: dict[str, Any] = dict(CONTRACT.document)
# Infrastructure routes every service has; they are not part of an API contract.
INFRASTRUCTURE = {"/health/live", "/health/ready", "/metrics"}


def service_routes() -> set[tuple[str, str]]:
    app = build_app(
        service="evaluation-service",
        version="test",
        health=Health(),
        tokens=None,
        audience="evaluation-service",
    )
    # Registering routes touches none of the dependencies.
    DatasetsAPI(
        store=cast(Store, None), simulation=cast(SimulationClient, None), log=get_logger("test")
    ).routes(app)
    return {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute) and route.path not in INFRASTRUCTURE
        for method in route.methods
    }


def operations() -> Iterator[tuple[str, str, dict[str, Any]]]:
    for path, item in DOC["paths"].items():
        for method, op in item.items():
            if method in ("get", "post", "put", "patch", "delete"):
                yield method.upper(), path, op


def test_operations_are_exactly_the_service_routes() -> None:
    documented = {(m, p) for m, p, _ in operations()}
    served = service_routes()
    assert served - documented == set(), "routes the contract does not document"
    assert documented - served == set(), "documented operations the service does not serve"
    assert len(served) == 6


def test_the_public_api_uses_the_edge_credentials() -> None:
    for method, path, op in operations():
        assert path.startswith("/api/v1/"), f"{method} {path}: the evaluation service has only a public API"
        assert "security" not in op, f"{method} {path}: the public API uses the edge's credentials"
