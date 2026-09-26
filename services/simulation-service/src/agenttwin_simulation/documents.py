"""Scenario and twin documents: parsing (YAML or JSON), validation and the
cross-checks between a scenario and its twin."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agenttwin.hashing import content_hash
from agenttwin_core.evaluators import Registry, expectation_problems
from agenttwin_core.schemas import document_validator
from agenttwin_core.yamlsafe import YAMLDocumentError, dump_yaml, load_yaml
from agenttwin_simulation.twin.definition import (
    MAX_STATE_BYTES,
    TwinDefinition,
    TwinDefinitionError,
    load_twin,
)
from agenttwin_simulation.twin.engine import merge_patch
from agenttwin_simulation.twin.faults import fault_problems

__all__ = [
    "DocumentProblem",
    "ScenarioCheck",
    "check_scenario",
    "export_yaml",
    "parse_document",
    "scenario_twin_problems",
    "spec_hash",
    "validate_twin",
]

MAX_DOCUMENT_BYTES = 512 * 1024


class DocumentProblem(ValueError):
    """The submitted document is unusable; ``problems`` lists why."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


def parse_document(body: Mapping[str, Any]) -> dict[str, Any]:
    """The document of a request body: ``{"document": {...}}`` or ``{"yaml": "..."}``."""
    has_doc, has_yaml = "document" in body, "yaml" in body
    if has_doc == has_yaml:
        raise DocumentProblem(["send exactly one of document (JSON) or yaml (text)"])
    if has_yaml:
        text = body["yaml"]
        if not isinstance(text, str):
            raise DocumentProblem(["yaml must be a string"])
        if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
            raise DocumentProblem([f"the document is larger than {MAX_DOCUMENT_BYTES} bytes"])
        try:
            doc = load_yaml(text, max_bytes=MAX_DOCUMENT_BYTES)
        except YAMLDocumentError as err:
            where = f" (line {err.line}, column {err.column})" if err.line is not None else ""
            raise DocumentProblem([f"invalid YAML{where}: {err}"]) from None
    else:
        doc = body["document"]
        if len(json.dumps(doc, default=str)) > MAX_DOCUMENT_BYTES:
            raise DocumentProblem([f"the document is larger than {MAX_DOCUMENT_BYTES} bytes"])
    if not isinstance(doc, dict):
        raise DocumentProblem(["the document must be an object"])
    return doc


def spec_hash(doc: Mapping[str, Any]) -> str:
    return content_hash(doc)


# Stored documents lose their author's key order (PostgreSQL's jsonb sorts
# object keys by length), so exports follow the reading order of a scenario
# or twin: these keys first, the rest alphabetically, ``critical`` last.
_FIRST_KEYS: dict[str, tuple[str, ...]] = {
    "": ("apiVersion", "kind", "metadata", "spec"),
    "metadata": ("name", "severity", "tags", "owner", "source", "description"),
    "spec": (
        # scenarios
        "agent",
        "twin",
        "seed",
        "covers",
        "input",
        "state",
        "faults",
        "expectations",
        # twins
        "tenantKey",
        "secrets",
        "retrieval",
        "initialState",
        "tools",
    ),
    "spec.input": ("message", "context", "documents"),
    "spec.faults[]": ("id", "target", "when", "behavior"),
    "spec.faults[].behavior": ("type",),
    "spec.expectations[]": ("id", "type"),
}
_LAST_KEYS: dict[str, tuple[str, ...]] = {"spec.expectations[]": ("critical",)}


def _reading_order(value: Any, path: str = "") -> Any:
    if isinstance(value, Mapping):
        first = [k for k in _FIRST_KEYS.get(path, ()) if k in value]
        last = [k for k in _LAST_KEYS.get(path, ()) if k in value and k not in first]
        keys = [*first, *sorted(k for k in value if k not in first and k not in last), *last]
        return {k: _reading_order(value[k], f"{path}.{k}" if path else str(k)) for k in keys}
    if isinstance(value, list):
        return [_reading_order(v, f"{path}[]") for v in value]
    return value


def export_yaml(doc: Mapping[str, Any]) -> str:
    """The document as YAML in reading order (see ``_FIRST_KEYS``)."""
    return dump_yaml(_reading_order(doc))


def _schema_problems(doc: Any, schema: str) -> list[str]:
    validator = document_validator(schema)
    out = []
    for e in sorted(validator.iter_errors(doc), key=lambda e: [str(p) for p in e.path])[:25]:
        where = "/".join(str(p) for p in e.path) or "(root)"
        out.append(f"{where}: {e.message}")
    return out


def validate_twin(doc: Mapping[str, Any], adapters: list[str] | None = None) -> TwinDefinition:
    try:
        return load_twin(doc, adapters=adapters or [])
    except TwinDefinitionError as err:
        raise DocumentProblem(err.problems) from None


@dataclass
class ScenarioCheck:
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.problems


def check_scenario(doc: Any, registry: Registry) -> ScenarioCheck:
    """Problems of a scenario on its own (schema, expectations, faults)."""
    check = ScenarioCheck(problems=_schema_problems(doc, "scenario.v1"))
    if check.problems:
        return check
    spec = doc["spec"]
    for i, exp in enumerate(spec["expectations"]):
        check.problems.extend(f"spec.expectations[{i}]: {p}" for p in expectation_problems(exp, registry))
    check.problems.extend(fault_problems(spec.get("faults")))
    if spec.get("simulationMode") == "approximate":
        check.problems.append(
            "spec.simulationMode: approximate (LLM-simulated) tools are not enabled in this deployment; "
            "use a declarative twin (strict mode)"
        )
    if not spec.get("twin"):
        check.problems.append("spec.twin: a simulation needs a tool twin (unknown tools fail closed)")
    if not spec.get("agent"):
        check.warnings.append("spec.agent is not set: the scenario applies to every agent of the project")
    semantic = [e for e in spec["expectations"] if e.get("type") == "semantic"]
    if semantic:
        check.warnings.append(
            f"{len(semantic)} semantic expectation(s) are graded by an evaluation run's judge; "
            "a simulation run alone leaves them SKIPPED"
        )
    return check


def scenario_twin_problems(doc: Mapping[str, Any], twin: TwinDefinition) -> ScenarioCheck:
    """Cross-checks a (valid) scenario against the twin it will run on."""
    check = ScenarioCheck()
    spec = doc["spec"]
    tools = set(twin.tools)
    check.problems.extend(fault_problems(spec.get("faults"), sorted(tools)))
    for name in spec.get("allowedTools") or []:
        if name not in tools:
            check.problems.append(f"spec.allowedTools: the twin {twin.name!r} has no tool {name!r}")
    for i, exp in enumerate(spec["expectations"]):
        tool = exp.get("tool")
        if tool and tool not in tools and exp.get("type") not in ("toolNotCalled",):
            check.warnings.append(f"spec.expectations[{i}]: the twin {twin.name!r} has no tool {tool!r}")
    state = merge_patch(twin.initial_state, spec.get("state") or {})
    if len(json.dumps(state, default=str)) > MAX_STATE_BYTES:
        check.problems.append(f"spec.state: the initial twin state is larger than {MAX_STATE_BYTES} bytes")
    return check
