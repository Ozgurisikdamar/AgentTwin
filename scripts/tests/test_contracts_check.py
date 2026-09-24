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
    # The real check against the committed baseline: every published schema
    # is compatible, and the baseline records all of them.
    assert cc.main([]) == 0
    baseline = json.loads(cc.BASELINE.read_text(encoding="utf-8"))
    assert set(baseline) == set(cc.current())
    assert "compatible with the baseline" in capsys.readouterr().out
