"""The OpenAPI contract checker (ADR-0021) on a small document: strict bodies,
path matching, status fallbacks, request checks, formats, embedded documents
and the webhook transport."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from agenttwin_core.openapi_contract import Contract, ContractTransport, ContractViolation, strictify

UUID = "0190f3b4-0000-7000-8000-000000000001"

DOC: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "t", "version": "1"},
    "paths": {
        "/things": {
            "get": {
                "operationId": "listThings",
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1}},
                    {"name": "X-Org", "in": "header", "schema": {"type": "string", "format": "uuid"}},
                ],
                "responses": {
                    "200": {"$ref": "#/components/responses/Things"},
                    "4XX": {"$ref": "#/components/responses/Error"},
                },
            },
            "post": {
                "operationId": "createThing",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/NewThing"}}},
                },
                "responses": {
                    "201": {
                        "description": "created",
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}},
                    },
                    "default": {"$ref": "#/components/responses/Error"},
                },
            },
        },
        "/things/special": {
            "get": {
                "operationId": "specialThing",
                "responses": {"204": {"description": "nothing"}},
            }
        },
        "/things/{thing_id}": {
            "parameters": [
                {
                    "name": "thing_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string", "format": "uuid"},
                }
            ],
            "get": {
                "operationId": "getThing",
                "responses": {
                    "200": {
                        "description": "one",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/ThingDetail"}}
                        },
                    }
                },
            },
        },
        "/raw": {
            "post": {
                "operationId": "raw",
                "responses": {
                    "default": {"description": "any", "content": {"application/json": {"schema": {}}}}
                },
            }
        },
    },
    "webhooks": {
        "ping": {
            "post": {
                "operationId": "ping",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["n"],
                                "properties": {"n": {"type": "integer"}},
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "pong",
                        "content": {
                            "application/json": {
                                "schema": {"type": "object", "properties": {"pong": {"type": "boolean"}}}
                            }
                        },
                    }
                },
            }
        }
    },
    "components": {
        "responses": {
            "Things": {
                "description": "things",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["items"],
                            "properties": {
                                "items": {"type": "array", "items": {"$ref": "#/components/schemas/Thing"}}
                            },
                        }
                    }
                },
            },
            "Error": {
                "description": "error",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["error"],
                            "properties": {
                                "error": {
                                    "type": "object",
                                    "properties": {"code": {"type": "string"}, "details": {"type": "object"}},
                                }
                            },
                        }
                    }
                },
            },
        },
        "schemas": {
            "Thing": {
                "type": "object",
                "required": ["id", "at"],
                "properties": {
                    "id": {"type": "string", "format": "uuid"},
                    "at": {"type": "string", "format": "date-time"},
                    "note": {"type": ["string", "null"]},
                },
            },
            "ThingDetail": {
                "allOf": [
                    {"$ref": "#/components/schemas/Thing"},
                    {
                        "type": "object",
                        "properties": {
                            "kind": {"anyOf": [{"$ref": "#/components/schemas/Kind"}, {"type": "null"}]},
                            "scenario": {"type": "object", "x-agenttwin-schema": "scenario.v1"},
                            "meta": {"type": "object", "additionalProperties": True, "properties": {"a": {}}},
                        },
                    },
                ]
            },
            "Kind": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}},
            "NewThing": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            },
        },
    },
}

THING = {"id": UUID, "at": "2026-09-25T10:00:00.5Z", "note": None}
SCENARIO = {
    "apiVersion": "agenttwin.dev/v1",
    "kind": "Scenario",
    "metadata": {"name": "s", "severity": "low"},
    "spec": {"input": {"message": "hi"}, "expectations": [{"type": "toolCalled", "tool": "t"}]},
}


def contract() -> Contract:
    return Contract(DOC, "things")


def body(value: Any) -> bytes:
    return json.dumps(value).encode()


def respond(c: Contract, method: str, path: str, status: int, value: Any) -> None:
    c.check_response(method, path, status, "application/json", body(value))


def test_strictify_closes_declared_objects_but_not_allof_members() -> None:
    schema = {
        "type": "object",
        "properties": {"a": {"type": "object", "properties": {"b": {}}}, "free": {"type": "object"}},
        "allOf": [{"properties": {"c": {}}}],
        "anyOf": [{"properties": {"d": {}}}, {"type": "null"}],
    }
    out = strictify(schema)
    assert out["unevaluatedProperties"] is False
    assert out["properties"]["a"]["unevaluatedProperties"] is False
    assert "unevaluatedProperties" not in out["properties"]["free"]  # free-form stays open
    assert "unevaluatedProperties" not in out["allOf"][0]
    assert out["anyOf"][0]["unevaluatedProperties"] is False
    explicit = strictify({"properties": {"a": {}}, "additionalProperties": True})
    assert "unevaluatedProperties" not in explicit
    assert schema["properties"]["a"] == {"type": "object", "properties": {"b": {}}}  # input untouched


def test_responses_are_checked_strictly_through_refs_and_allof() -> None:
    c = contract()
    respond(c, "GET", "/things", 200, {"items": [THING]})
    respond(c, "GET", f"/things/{UUID}", 200, THING | {"kind": {"name": "k"}, "meta": {"a": 1, "z": 2}})
    with pytest.raises(ContractViolation, match=r"at /items/0: Unevaluated properties.*'extra'"):
        respond(c, "GET", "/things", 200, {"items": [THING | {"extra": 1}]})
    with pytest.raises(ContractViolation, match=r"'extra' was unexpected"):
        respond(c, "GET", f"/things/{UUID}", 200, THING | {"extra": 1})
    with pytest.raises(ContractViolation, match=r"at /kind"):
        respond(c, "GET", f"/things/{UUID}", 200, THING | {"kind": {"name": "k", "x": 1}})
    with pytest.raises(ContractViolation, match="'id' is a required property"):
        respond(c, "GET", "/things", 200, {"items": [{"at": THING["at"]}]})


def test_formats_are_enforced() -> None:
    c = contract()
    for bad in (
        {"id": UUID.upper()},
        {"id": "not-a-uuid"},
        {"at": "2026-09-25 10:00:00"},
        {"at": "2026-02-30T10:00:00Z"},
    ):
        with pytest.raises(ContractViolation):
            respond(c, "GET", "/things", 200, {"items": [THING | bad]})
    respond(c, "GET", "/things", 200, {"items": [THING | {"at": "2026-09-25T10:00:00+03:00"}]})


def test_embedded_documents_are_checked_against_their_schema() -> None:
    c = contract()
    respond(c, "GET", f"/things/{UUID}", 200, THING | {"scenario": SCENARIO})
    broken = SCENARIO | {"metadata": {"name": "s", "severity": "urgent"}}
    with pytest.raises(ContractViolation, match=r"is not a valid scenario\.v1 document: metadata/severity"):
        respond(c, "GET", f"/things/{UUID}", 200, THING | {"scenario": broken})


def test_literal_paths_win_and_statuses_fall_back_to_ranges_and_default() -> None:
    c = contract()
    found = c.find("GET", "/things/special")
    assert found is not None and found[0].operation_id == "specialThing"
    found = c.find("GET", f"/things/{UUID}")
    assert found is not None and found[1] == {"thing_id": UUID}
    assert c.find("GET", "/things//") is None and c.find("DELETE", "/things") is None
    c.check_response("GET", "/things/special", 204, None, b"")
    with pytest.raises(ContractViolation, match="no body is documented"):
        c.check_response("GET", "/things/special", 204, "application/json", b"{}")
    respond(c, "GET", "/things", 404, {"error": {"code": "NOT_FOUND"}})  # 4XX
    respond(c, "POST", "/things", 503, {"error": {"code": "UNAVAILABLE"}})  # default
    with pytest.raises(ContractViolation, match="answered 500, which is not documented"):
        respond(c, "GET", "/things", 500, {"error": {}})
    with pytest.raises(ContractViolation, match="GET /nothing is not a documented operation"):
        respond(c, "GET", "/nothing", 200, {})
    with pytest.raises(ContractViolation, match="content type text/plain is not documented"):
        c.check_response("GET", "/things", 200, "text/plain", b"x")
    with pytest.raises(ContractViolation, match="the body is not JSON"):
        c.check_response("GET", "/things", 200, "application/json", b"{nope")
    # An "any" schema accepts any body, even one that is not JSON.
    c.check_response("POST", "/raw", 502, "application/json", b"{nope")


def test_accepted_requests_must_match_the_documented_parameters_and_body() -> None:
    c = contract()
    c.check_request("GET", "/things", query=[("limit", "5")], headers={"x-org": UUID, "authorization": "x"})
    c.check_request("POST", "/things", content_type="application/json", body=body({"name": "n"}))
    with pytest.raises(ContractViolation, match="undocumented query parameter 'q'"):
        c.check_request("GET", "/things", query=[("q", "x")])
    with pytest.raises(ContractViolation, match="query parameter 'limit'='0'"):
        c.check_request("GET", "/things", query=[("limit", "0")])
    with pytest.raises(ContractViolation, match="header parameter 'x-org'"):
        c.check_request("GET", "/things", headers={"X-Org": "nope"})
    with pytest.raises(ContractViolation, match="path parameter 'thing_id'"):
        c.check_request("GET", "/things/nope")
    with pytest.raises(ContractViolation, match="requires a request body"):
        c.check_request("POST", "/things")
    with pytest.raises(ContractViolation, match="Additional properties are not allowed"):
        c.check_request("POST", "/things", content_type="application/json", body=body({"name": "n", "x": 1}))
    with pytest.raises(ContractViolation, match="documents no request body"):
        c.check_request("GET", "/things", body=b"{}")


def test_coverage_counts_successful_answers_only() -> None:
    c = contract()
    assert c.uncovered() == ["createThing", "getThing", "listThings", "ping", "raw", "specialThing"]
    respond(c, "GET", "/things", 200, {"items": []})
    respond(c, "POST", "/things", 400, {"error": {"code": "INVALID_REQUEST"}})
    assert "listThings" not in c.uncovered() and "createThing" in c.uncovered()


def test_check_schema_and_load(tmp_path: Path) -> None:
    c = contract()
    c.check_schema("Thing", THING)
    with pytest.raises(ContractViolation, match="not a valid Thing"):
        c.check_schema("Thing", {"id": UUID})
    path = tmp_path / "things.openapi.yaml"
    path.write_text(json.dumps(DOC))
    assert Contract.load(path).name == "things"
    path.write_text(json.dumps(DOC | {"openapi": "3.0.3"}))
    with pytest.raises(ValueError, match=r"not an OpenAPI 3\.1 document"):
        Contract.load(path)
    cyclic = DOC | {"components": {"schemas": {"Thing": {"$ref": "#/components/schemas/Thing"}}}}
    with pytest.raises(ValueError, match="cyclic"):
        Contract(cyclic).check_schema("Thing", {})


@pytest.mark.anyio
async def test_webhook_calls_are_checked_by_the_transport() -> None:
    answers = {"n": 1}

    def agent(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"pong": True} if answers["n"] else {"pong": "yes"})

    c = contract()
    transport = ContractTransport(c, "ping", httpx.MockTransport(agent))
    async with httpx.AsyncClient(transport=transport) as client:
        r = await client.post("http://agent/run", json={"n": 1})
        assert r.json() == {"pong": True}
        assert c.uncovered() == ["createThing", "getThing", "listThings", "raw", "specialThing"]
        with pytest.raises(ContractViolation, match="ping request"):
            await client.post("http://agent/run", json={"n": "one"})
        answers["n"] = 0
        with pytest.raises(ContractViolation, match="ping 200 response"):
            await client.post("http://agent/run", json={"n": 2})
    assert len(c.violations) == 2
