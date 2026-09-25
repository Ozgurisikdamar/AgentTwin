"""Twin definitions (``TwinDefinition`` documents, twin.v1 schema): loading
and the semantic checks the JSON schema cannot express."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from agenttwin_core.compare import COMPARATORS
from agenttwin_core.jsonschema_safe import schema_problem
from agenttwin_core.schemas import document_validator
from agenttwin_simulation.twin.templates import check_template

__all__ = [
    "CONDITION_ROOTS",
    "Condition",
    "Effect",
    "ErrorSpec",
    "Fixture",
    "Handler",
    "ToolDef",
    "TwinDefinition",
    "TwinDefinitionError",
    "load_twin",
]

HandlerKind = Literal["read", "mutate", "static", "echo", "recorded", "custom"]
CONDITION_ROOTS = ("args", "state", "value", "tenant")
MAX_STATE_BYTES = 1 << 20


class TwinDefinitionError(ValueError):
    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("invalid twin definition: " + "; ".join(self.problems))


@dataclass(frozen=True)
class ErrorSpec:
    status: int
    code: str
    message: str

    @staticmethod
    def parse(raw: Mapping[str, Any] | None, default: ErrorSpec) -> ErrorSpec:
        if not raw:
            return default
        return ErrorSpec(int(raw["status"]), str(raw["code"]), str(raw.get("message") or default.message))


@dataclass(frozen=True)
class Condition:
    path: str
    comparators: Mapping[str, Any]
    error: ErrorSpec | None


@dataclass(frozen=True)
class Effect:
    op: str
    path: str
    value: Any = None
    by: Any = 1


@dataclass(frozen=True)
class Fixture:
    match: Mapping[str, Any]
    status: int
    response: Any
    error: ErrorSpec | None


@dataclass(frozen=True)
class Handler:
    kind: HandlerKind
    path: str | None = None
    not_found: ErrorSpec = ErrorSpec(404, "NOT_FOUND", "The requested record does not exist.")
    preconditions: tuple[Condition, ...] = ()
    effects: tuple[Effect, ...] = ()
    response: Any = None
    has_response: bool = False
    fixtures: tuple[Fixture, ...] = ()
    adapter: str | None = None


@dataclass(frozen=True)
class ToolDef:
    name: str
    handler: Handler
    description: str = ""
    risk: str = "WRITE_IRREVERSIBLE"
    risk_declared: bool = False
    input_schema: Mapping[str, Any] | None = None
    output_schema: Mapping[str, Any] | None = None
    idempotency_argument: str | None = None
    idempotency_header: str | None = None
    effect_key: str | None = None
    tenant_path: str | None = None

    @property
    def expects_mutation(self) -> bool:
        return self.handler.kind == "mutate" and bool(self.handler.effects)


@dataclass(frozen=True)
class TwinDefinition:
    name: str
    tools: Mapping[str, ToolDef]
    description: str = ""
    source: str = "declarative"
    secrets: tuple[str, ...] = ()
    tenant_key: str | None = None
    initial_state: Mapping[str, Any] = field(default_factory=dict)
    retrieval_path: str = "kb"

    def risks(self) -> dict[str, str]:
        return {name: t.risk for name, t in self.tools.items()}


def _schema_errors(doc: Any) -> list[str]:
    validator = document_validator("twin.v1")
    out = []
    for e in sorted(validator.iter_errors(doc), key=lambda e: [str(p) for p in e.path])[:20]:
        where = "/".join(str(p) for p in e.path) or "(root)"
        out.append(f"{where}: {e.message}")
    return out


def _condition(raw: Mapping[str, Any], where: str, problems: list[str]) -> Condition:
    path = str(raw["path"])
    root = path.split(".", 1)[0].split("[", 1)[0]
    if root not in CONDITION_ROOTS:
        problems.append(f"{where}.path must start with one of {', '.join(CONDITION_ROOTS)}; got {path!r}")
    if problem := check_template(path, path=True):
        problems.append(f"{where}.path: {problem}")
    comparators = {k: raw[k] for k in COMPARATORS if k in raw}
    for k, v in comparators.items():
        if problem := check_template(v):
            problems.append(f"{where}.{k}: {problem}")
    error = (
        ErrorSpec.parse(raw.get("error"), ErrorSpec(409, "PRECONDITION_FAILED", ""))
        if raw.get("error")
        else None
    )
    return Condition(path=path, comparators=comparators, error=error)


def _effect(raw: Mapping[str, Any], where: str, problems: list[str]) -> Effect:
    op = str(raw["op"])
    path = str(raw["path"])
    if problem := check_template(path, path=True):
        problems.append(f"{where}.path: {problem}")
    if op in ("set", "append", "merge") and "value" not in raw:
        problems.append(f"{where}: op {op} needs a value")
    if op == "merge" and "value" in raw and not isinstance(raw["value"], Mapping | str):
        problems.append(f"{where}: op merge needs an object value")
    if "value" in raw and (problem := check_template(raw["value"])):
        problems.append(f"{where}.value: {problem}")
    by = raw.get("by", 1)
    if isinstance(by, str) and (problem := check_template(by)):
        problems.append(f"{where}.by: {problem}")
    return Effect(op=op, path=path, value=raw.get("value"), by=by)


def _handler(raw: Mapping[str, Any], where: str, problems: list[str], adapters: Sequence[str]) -> Handler:
    kind = raw["kind"]
    path = raw.get("path")
    if path is not None and (problem := check_template(str(path), path=True)):
        problems.append(f"{where}.path: {problem}")
    if kind == "read" and not path:
        problems.append(f"{where}: a read handler needs a path")
    pre = tuple(
        _condition(c, f"{where}.preconditions[{i}]", problems)
        for i, c in enumerate(raw.get("preconditions") or [])
    )
    effects = tuple(
        _effect(e, f"{where}.effects[{i}]", problems) for i, e in enumerate(raw.get("effects") or [])
    )
    if kind == "mutate" and not effects:
        problems.append(f"{where}: a mutate handler needs at least one effect")
    if kind != "mutate" and (effects or pre):
        problems.append(f"{where}: only mutate handlers have preconditions and effects")
    if "response" in raw and (problem := check_template(raw["response"])):
        problems.append(f"{where}.response: {problem}")
    fixtures: list[Fixture] = []
    for i, f in enumerate(raw.get("fixtures") or []):
        err = ErrorSpec.parse(f.get("error"), ErrorSpec(500, "ERROR", "")) if f.get("error") else None
        fixtures.append(
            Fixture(
                match=dict(f.get("match") or {}),
                status=int(f.get("status", 200)),
                response=f.get("response"),
                error=err,
            )
        )
        if "response" in f and (problem := check_template(f["response"])):
            problems.append(f"{where}.fixtures[{i}].response: {problem}")
    if kind == "recorded" and not fixtures:
        problems.append(f"{where}: a recorded handler needs fixtures")
    adapter = raw.get("adapter")
    if kind == "custom":
        if not adapter:
            problems.append(f"{where}: a custom handler needs an adapter")
        elif adapter not in adapters:
            problems.append(
                f"{where}: unknown adapter {adapter!r} (registered: {', '.join(adapters) or 'none'})"
            )
    return Handler(
        kind=kind,
        path=str(path) if path is not None else None,
        not_found=ErrorSpec.parse(raw.get("notFound"), Handler.not_found),
        preconditions=pre,
        effects=effects,
        response=raw.get("response"),
        has_response="response" in raw,
        fixtures=tuple(fixtures),
        adapter=str(adapter) if adapter else None,
    )


def _risk(raw: Any) -> str | None:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, Mapping) and isinstance(raw.get("level"), str):
        return str(raw["level"])
    return None


def load_twin(doc: Any, *, adapters: Sequence[str] = ()) -> TwinDefinition:
    """Validates a twin document and returns its definition.

    Raises :class:`TwinDefinitionError` listing every problem found."""
    problems = _schema_errors(doc)
    if problems:
        raise TwinDefinitionError(problems)
    spec = doc["spec"]
    initial = spec.get("initialState") or {}
    if len(json.dumps(initial, default=str)) > MAX_STATE_BYTES:
        problems.append(f"spec.initialState is larger than {MAX_STATE_BYTES} bytes")
    tools: dict[str, ToolDef] = {}
    for name, raw in sorted(spec["tools"].items()):
        where = f"spec.tools.{name}"
        handler = _handler(raw["handler"], f"{where}.handler", problems, adapters)
        for key in ("inputSchema", "outputSchema"):
            if key in raw and (problem := schema_problem(raw[key])):
                problems.append(f"{where}.{key}: {problem}")
        for key in ("effectKey",):
            if key in raw and (problem := check_template(raw[key])):
                problems.append(f"{where}.{key}: {problem}")
        if "tenantPath" in raw and (problem := check_template(raw["tenantPath"], path=True)):
            problems.append(f"{where}.tenantPath: {problem}")
        declared = raw.get("risk")
        risk = declared or ("READ" if handler.kind == "read" else "WRITE_IRREVERSIBLE")
        idem = raw.get("idempotency") or {}
        tools[name] = ToolDef(
            name=name,
            handler=handler,
            description=str(raw.get("description") or ""),
            risk=str(risk),
            risk_declared=declared is not None,
            input_schema=raw.get("inputSchema"),
            output_schema=raw.get("outputSchema"),
            idempotency_argument=idem.get("argument"),
            idempotency_header=(idem.get("header") or "").lower() or None,
            effect_key=raw.get("effectKey"),
            tenant_path=raw.get("tenantPath"),
        )
    if problems:
        raise TwinDefinitionError(problems)
    retrieval = spec.get("retrieval") or {}
    return TwinDefinition(
        name=doc["metadata"]["name"],
        description=str(doc["metadata"].get("description") or ""),
        source=str(doc["metadata"].get("source") or "declarative"),
        tools=tools,
        secrets=tuple(spec.get("secrets") or ()),
        tenant_key=spec.get("tenantKey"),
        initial_state=initial,
        retrieval_path=str(retrieval.get("statePath") or "kb"),
    )


def risk_of(raw: Any) -> str | None:
    """The risk level of a manifest tool entry (a string or ``{level: ...}``)."""
    return _risk(raw)
