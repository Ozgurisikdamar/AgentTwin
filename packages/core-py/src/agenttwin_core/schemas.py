"""Access to the shared JSON Schema contracts (ADR-0010).

The schemas live once in the repository (``packages/contracts`` and
``packages/scenario-schema``) and are read from there by every language;
there are no copies that could drift. Container images copy both directories
and point ``AGENTTWIN_SCHEMA_DIR`` at their parent.
"""

from __future__ import annotations

import json
import os
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

__all__ = [
    "document_definition_validator",
    "document_validator",
    "event_payload_validator",
    "load_topology",
    "schema_root",
]


@cache
def schema_root() -> Path:
    """The directory holding ``contracts/`` and ``scenario-schema/``."""
    env = os.environ.get("AGENTTWIN_SCHEMA_DIR")
    if env:
        root = Path(env)
        if not (root / "contracts" / "topology.json").is_file():
            raise FileNotFoundError(f"AGENTTWIN_SCHEMA_DIR={env} does not contain contracts/topology.json")
        return root
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "packages"
        if (candidate / "contracts" / "topology.json").is_file():
            return candidate
    raise FileNotFoundError("schema contracts not found; set AGENTTWIN_SCHEMA_DIR")


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@cache
def load_topology() -> dict[str, Any]:
    topo: dict[str, Any] = _read(schema_root() / "contracts" / "topology.json")
    if not topo.get("exchange") or int(topo.get("max_attempts", 0)) < 1 or not topo.get("queues"):
        raise ValueError("topology contract is incomplete")
    return topo


@cache
def _document_schema(name: str) -> dict[str, Any]:
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"invalid schema name {name!r}")
    schema: dict[str, Any] = _read(schema_root() / "scenario-schema" / "schemas" / f"{name}.schema.json")
    Draft202012Validator.check_schema(schema)
    return schema


@cache
def document_validator(name: str) -> Draft202012Validator:
    """Validator for a user-authored document schema, e.g. ``scenario.v1``."""
    return Draft202012Validator(_document_schema(name), format_checker=FormatChecker())


@cache
def document_definition_validator(name: str, definition: str) -> Draft202012Validator:
    """Validator for one definition (``$defs``) of a document schema, e.g. the
    ``fault`` rule of ``scenario.v1``. References between definitions resolve
    as they do in the whole document."""
    defs = _document_schema(name).get("$defs") or {}
    if definition not in defs:
        raise KeyError(f"{name} has no definition {definition!r}")
    return Draft202012Validator(
        {"$ref": f"#/$defs/{definition}", "$defs": defs}, format_checker=FormatChecker()
    )


@cache
def event_payload_validator(event_type: str) -> Draft202012Validator:
    """Validator for the payload of ``event_type`` (``envelope.v1`` for the envelope)."""
    if "/" in event_type or "\\" in event_type or ".." in event_type:
        raise ValueError(f"invalid event type {event_type!r}")
    path = schema_root() / "contracts" / "events" / f"{event_type}.schema.json"
    if not path.is_file():
        raise KeyError(f"unknown event type {event_type!r}")
    schema = _read(path)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())
