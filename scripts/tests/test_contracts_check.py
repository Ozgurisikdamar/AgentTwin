"""The event-schema compatibility check (make contracts-check)."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "contracts_check", Path(__file__).resolve().parent.parent / "contracts_check.py"
)
assert _SPEC and _SPEC.loader
cc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cc)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["id", "status"],
    "properties": {
        "id": {"type": "string"},
        "status": {"type": "string", "enum": ["OK", "ERROR"]},
        "note": {"type": ["string", "null"]},
        "agent": {"$ref": "#/$defs/agentRef"},
        "tags": {
            "type": "array",
            "items": {"type": "object", "required": ["k"], "properties": {"k": {"type": "string"}}},
        },
    },
    "$defs": {
        "agentRef": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}
    },
}


def check(new: dict[str, Any]) -> list[str]:
    base = {"e.v1": cc.fingerprint(SCHEMA)}
    return cc.breaking_changes(base, {"e.v1": cc.fingerprint(new)})


def test_fingerprint_resolves_refs_and_arrays() -> None:
    fp = cc.fingerprint(SCHEMA)
    assert fp["required"] == {"/": ["id", "status"], "/agent": ["name"], "/tags/[]": ["k"]}
    assert fp["types"]["/note"] == ["null", "string"]
    assert fp["enums"]["/status"] == ['"ERROR"', '"OK"']


def test_additive_changes_are_compatible() -> None:
    new = copy.deepcopy(SCHEMA)
    new["properties"]["extra"] = {"type": "integer"}
    new["properties"]["nested"] = {
        "type": "object",
        "required": ["x"],
        "properties": {"x": {"type": "string"}},
    }
    new["properties"]["status"]["enum"].append("PARTIAL")
    del new["properties"]["note"]  # optional field
    assert check(new) == []


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda s: s["required"].remove("status"), "required field /status was removed or made optional"),
        (lambda s: s["required"].append("note"), "existing field /note became required"),
        (lambda s: s["required"].append("brand_new"), "new required field /brand_new"),
        (lambda s: s["properties"]["id"].update(type="integer"), "type of /id changed"),
        (
            lambda s: s["properties"]["status"]["enum"].remove("ERROR"),
            'enum values removed at /status: "ERROR"',
        ),
        (lambda s: s["$defs"]["agentRef"]["required"].clear(), "required field /agent/name was removed"),
        (
            lambda s: s["properties"]["tags"]["items"]["required"].clear(),
            "required field /tags/[]/k was removed",
        ),
    ],
)
def test_breaking_changes_are_reported(mutate: Any, expected: str) -> None:
    new = copy.deepcopy(SCHEMA)
    mutate(new)
    problems = check(new)
    assert any(expected in p for p in problems), problems


def test_removed_event_type_is_breaking() -> None:
    assert cc.breaking_changes({"e.v1": cc.fingerprint(SCHEMA)}, {}) == [
        "e.v1: published schema was removed (publish a new version instead)"
    ]


def test_committed_baseline_matches_the_repository(capsys: pytest.CaptureFixture[str]) -> None:
    # The real check against the committed baselines: every published schema
    # and API document is compatible, and the baselines record all of them.
    assert cc.main([]) == 0
    baseline = json.loads(cc.BASELINE.read_text(encoding="utf-8"))
    assert set(baseline) == set(cc.current())
    apis = json.loads(cc.OPENAPI_BASELINE.read_text(encoding="utf-8"))
    assert apis == cc.current_openapi()  # up to date, not only compatible
    out = capsys.readouterr().out
    assert "event schemas compatible with the baseline" in out
    assert "API documents (17 operations) compatible with the baseline" in out


# ---------------------------------------------------------------- OpenAPI

API: dict[str, Any] = {
    "openapi": "3.1.0",
    "paths": {
        "/runs": {
            "get": {
                "parameters": [
                    {"name": "status", "in": "query", "schema": {"$ref": "#/components/schemas/Status"}}
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/RunDetail"}}
                        }
                    },
                    "404": {"content": {"application/json": {"schema": {"type": "object"}}}},
                },
            },
            "post": {
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["agent"],
                                "properties": {"agent": {"type": "string"}, "seed": {"type": "integer"}},
                            }
                        }
                    },
                },
                "responses": {
                    "202": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Run"}}}}
                },
            },
        }
    },
    "webhooks": {
        "agentRun": {
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["input"],
                                "properties": {"input": {"type": "string"}, "context": {"type": "object"}},
                            }
                        }
                    }
                },
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"type": "object", "properties": {"output": {"type": "string"}}}
                            }
                        }
                    }
                },
            }
        }
    },
    "components": {
        "schemas": {
            "Status": {"type": "string", "enum": ["QUEUED", "DONE"]},
            "Run": {
                "type": "object",
                "required": ["id", "status"],
                "properties": {
                    "id": {"type": "string"},
                    "status": {"$ref": "#/components/schemas/Status"},
                    "error": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
            },
            "RunDetail": {
                "allOf": [
                    {"$ref": "#/components/schemas/Run"},
                    {"type": "object", "required": ["cases"], "properties": {"cases": {"type": "array"}}},
                ]
            },
        }
    },
}


def api_check(new: dict[str, Any]) -> list[str]:
    old = {"svc": cc.openapi_fingerprint(API)}
    return cc.openapi_breaking_changes(old, {"svc": cc.openapi_fingerprint(new)})


def test_openapi_fingerprint_merges_allof_and_reads_null_unions() -> None:
    ops = cc.openapi_fingerprint(API)
    assert set(ops) == {"GET /runs", "POST /runs", "WEBHOOK agentRun"}
    detail = ops["GET /runs"]["responses"]["200"]
    assert detail["required"]["/"] == ["cases", "id", "status"]
    assert detail["types"]["/error"] == ["null", "string"]
    assert "404" not in ops["GET /runs"]["responses"]  # only success responses are a promise
    # The same document written flat instead of with allOf has the same fingerprint.
    flat = copy.deepcopy(API)
    run = flat["components"]["schemas"]["Run"]
    flat["components"]["schemas"]["RunDetail"] = {
        "type": "object",
        "required": ["id", "status", "cases"],
        "properties": {**run["properties"], "cases": {"type": "array"}},
    }
    assert api_check(flat) == []


def test_openapi_additive_changes_are_compatible() -> None:
    new = copy.deepcopy(API)
    schemas = new["components"]["schemas"]
    schemas["Status"]["enum"].append("PAUSED")  # a new value clients do not know yet
    schemas["Run"]["properties"]["note"] = {"type": "string"}
    new["paths"]["/runs"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"][
        "tags"
    ] = {"type": "array"}
    new["paths"]["/runs"]["get"]["parameters"].append(
        {"name": "limit", "in": "query", "schema": {"type": "integer"}}
    )
    new["paths"]["/other"] = {"get": {"responses": {"200": {"description": "x"}}}}
    webhook = new["webhooks"]["agentRun"]["post"]
    webhook["requestBody"]["content"]["application/json"]["schema"]["properties"]["tenant"] = {
        "type": "string"
    }
    del webhook["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["output"]
    assert api_check(new) == []


def _post_body(doc: dict[str, Any]) -> dict[str, Any]:
    schema: dict[str, Any] = doc["paths"]["/runs"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]
    return schema


def _hook(doc: dict[str, Any], part: str) -> dict[str, Any]:
    op = doc["webhooks"]["agentRun"]["post"]
    node = op["requestBody"] if part == "request" else op["responses"]["200"]
    schema: dict[str, Any] = node["content"]["application/json"]["schema"]
    return schema


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda d: d["paths"]["/runs"].pop("post"), "POST /runs: the operation was removed"),
        (lambda d: d["paths"]["/runs"]["get"]["parameters"].clear(), "parameter query:status was removed"),
        (
            lambda d: d["paths"]["/runs"]["get"]["parameters"][0].update(required=True),
            "parameter query:status is now required",
        ),
        (
            lambda d: d["components"]["schemas"]["Status"]["enum"].remove("DONE"),
            'GET /runs parameter query:status: enum values removed at /: "DONE"',
        ),
        (lambda d: _post_body(d)["properties"].pop("seed"), "POST /runs request: field /seed was removed"),
        (lambda d: _post_body(d)["required"].append("seed"), "POST /runs request: /seed became required"),
        (
            lambda d: _post_body(d)["properties"]["seed"].update(type="string"),
            "POST /runs request: type of /seed changed",
        ),
        (
            lambda d: d["components"]["schemas"]["Run"]["required"].remove("status"),
            "GET /runs 200 response: required field /status was removed or made optional",
        ),
        (
            lambda d: d["components"]["schemas"]["Run"]["properties"]["id"].update(type="integer"),
            "POST /runs 202 response: type of /id changed",
        ),
        (
            lambda d: d["paths"]["/runs"]["post"]["responses"].pop("202"),
            "POST /runs: success response 202 was removed",
        ),
        (
            lambda d: _hook(d, "request")["required"].clear(),
            "WEBHOOK agentRun request: required field /input was removed or made optional",
        ),
        (
            lambda d: _hook(d, "response").update(required=["output"]),
            "WEBHOOK agentRun 200 response: /output became required",
        ),
    ],
)
def test_openapi_breaking_changes_are_reported(mutate: Any, expected: str) -> None:
    new = copy.deepcopy(API)
    mutate(new)
    problems = api_check(new)
    assert any(expected in p for p in problems), problems


def test_a_removed_api_document_is_breaking() -> None:
    old = {"svc": cc.openapi_fingerprint(API)}
    assert cc.openapi_breaking_changes(old, {}) == ["svc: the published API document was removed"]
