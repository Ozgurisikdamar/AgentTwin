"""Every HTTP API document (``packages/contracts/openapi``, ADR-0021) as a
document: valid OpenAPI 3.1 that follows the API conventions. Whether each
service answers as documented is checked by its own tests (route parity and
traffic); this file holds the documents to the rules they share."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from openapi_spec_validator import validate
from openapi_spec_validator.readers import read_from_filename

REPO = Path(__file__).resolve().parents[2]
DOCUMENTS = sorted((REPO / "packages" / "contracts" / "openapi").glob("*.openapi.yaml"))
METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
MUTATIONS = ("POST", "PUT", "PATCH", "DELETE")
# The service every public request passes through; the others are reached
# through its proxy, which answers 502 and 503 for them.
EDGE = "control-plane"


def load(path: Path) -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load(path.read_text())
    return doc


def service(path: Path) -> str:
    return path.name.removesuffix(".openapi.yaml")


def pointer(doc: dict[str, Any], ref: str) -> Any:
    node: Any = doc
    for part in ref.removeprefix("#/").split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def follow(doc: dict[str, Any], node: Any) -> Any:
    while isinstance(node, dict) and isinstance(node.get("$ref"), str):
        node = pointer(doc, node["$ref"])
    return node


def operations(doc: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any], list[dict[str, Any]]]]:
    """(method, path, operation, its parameters with the path item's)."""
    for path, item in doc["paths"].items():
        shared = [follow(doc, p) for p in item.get("parameters", [])]
        for method in METHODS:
            if method in item:
                op = item[method]
                own = [follow(doc, p) for p in op.get("parameters", [])]
                yield method.upper(), path, op, shared + own


def refs(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str):
            yield node["$ref"]
        for value in node.values():
            yield from refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from refs(value)


def closed(doc: dict[str, Any], schema: Any) -> bool:
    """A body schema that rejects fields it does not declare: closed itself,
    every variant closed, or an embedded document checked against its
    canonical schema."""
    schema = follow(doc, schema)
    if schema.get("additionalProperties") is False or "x-agenttwin-schema" in schema:
        return True
    variants = schema.get("oneOf") or schema.get("anyOf")
    return bool(variants) and all(closed(doc, v) for v in variants)


@pytest.fixture(params=DOCUMENTS, ids=service)
def document(request: pytest.FixtureRequest) -> Path:
    path: Path = request.param
    return path


def test_there_is_a_document_per_service_api() -> None:
    assert [service(p) for p in DOCUMENTS] == [
        "control-plane",
        "evaluation-service",
        "simulation-service",
        "trace-service",
    ]


def test_the_document_is_valid_openapi_31(document: Path) -> None:
    spec, base = read_from_filename(str(document))
    validate(spec, base_uri=base)
    assert spec["openapi"] == "3.1.0"


def test_operation_ids_are_unique_across_documents() -> None:
    # Coverage, the client fakes and the web types name operations by id alone.
    seen: dict[str, str] = {}
    for path in DOCUMENTS:
        for method, route, op, _ in operations(load(path)):
            where = f"{service(path)} {method} {route}"
            assert op["operationId"] not in seen, (
                f"{where} reuses {op['operationId']} ({seen.get(op['operationId'])})"
            )
            seen[op["operationId"]] = where


def test_every_operation_follows_the_conventions(document: Path) -> None:
    doc = load(document)
    security = doc.get("security")
    for method, path, op, params in operations(doc):
        where = f"{method} {path}"
        assert op.get("summary") and op.get("tags"), f"{where}: a summary and a tag"
        declared = {p["name"] for p in params if p["in"] == "path"}
        assert declared == set(re.findall(r"\{(\w+)\}", path)), f"{where}: path parameters"
        responses = {str(k) for k in op["responses"]}
        for status, response in op["responses"].items():
            if str(status).startswith("2") and str(status) != "204":
                assert follow(doc, response).get("content"), f"{where}: {status} has a body"
        if not path.startswith(("/api/", "/internal/")):
            continue  # a receiver with its own protocol (OTLP, the twin endpoint)
        authenticated = op.get("security", security) != []
        body = follow(doc, op.get("requestBody")) if op.get("requestBody") else None
        assert "500" in responses, f"{where}: 500"
        if path.startswith("/api/"):
            assert "429" in responses, f"{where}: 429 (rate limits)"
            if service(document) != EDGE:
                assert {"502", "503"} <= responses, f"{where}: 502 and 503 (behind the edge's proxy)"
        if authenticated:
            assert {"401", "403"} <= responses, f"{where}: 401 and 403"
        if body or any(p["in"] in ("path", "query") for p in params):
            assert "400" in responses, f"{where}: 400"
        if declared:
            assert "404" in responses, f"{where}: 404"
        if body:
            assert "413" in responses, f"{where}: 413 (bodies are bounded)"
            json_schema = body["content"].get("application/json", {}).get("schema")
            if json_schema is not None:
                assert closed(doc, json_schema), (
                    f"{where}: the service rejects unknown fields; the body says so"
                )
        if method in MUTATIONS and path.startswith("/api/") and authenticated:
            names = {p["name"] for p in params if p["in"] == "header"}
            assert "Idempotency-Key" in names, f"{where}: Idempotency-Key"
            assert {"409", "422"} <= responses, f"{where}: 409 and 422 (idempotency)"


def test_every_component_is_used(document: Path) -> None:
    doc = load(document)
    used = set(refs({k: v for k, v in doc.items() if k != "components"}))
    # Components referenced only by other components count once reached.
    frontier = list(used)
    while frontier:
        for ref in refs(pointer(doc, frontier.pop())):
            if ref not in used:
                used.add(ref)
                frontier.append(ref)
    for kind, items in doc["components"].items():
        if kind == "securitySchemes":
            continue
        for name in items:
            assert f"#/components/{kind}/{name}" in used, f"unused component {kind}/{name}"
