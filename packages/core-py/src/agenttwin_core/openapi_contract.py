"""Checks HTTP traffic against an OpenAPI 3.1 contract (ADR-0021).

The documents in ``packages/contracts/openapi`` describe the APIs the services
publish. The integration tests pass every response a service gives — and every
request it accepts — through :class:`Contract`, so the document cannot drift
from the implementation unnoticed:

* the operation must exist (method and path template) and the status must be
  documented for it (the exact code, a ``2XX``-style range or ``default``);
* JSON bodies must match the documented schema **strictly**: an object that
  declares its properties may not carry undocumented ones. The published
  document stays open for evolution (clients ignore unknown fields); the check
  makes a field the server sends without documenting it fail the test that
  sees it;
* ``x-agenttwin-schema: <name>`` marks an embedded document (a scenario, a
  twin definition), which must also be valid against that canonical schema;
  ``<name>#/$defs/<definition>`` marks a part of one (a scenario's fault rule);
* accepted requests must match the documented parameters and body, so a client
  cannot depend on something the contract does not promise;
* every checked exchange is recorded, so a test can require that each
  operation was exercised (:meth:`Contract.uncovered`).
"""

from __future__ import annotations

import ast
import json
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from jsonschema.protocols import Validator
from jsonschema.validators import extend

from agenttwin_core.schemas import document_definition_validator, document_validator, schema_root
from agenttwin_core.yamlsafe import load_yaml

__all__ = [
    "Contract",
    "ContractTransport",
    "ContractViolation",
    "Operation",
    "contract_path",
    "embedded_validator",
    "strictify",
]

METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
# Keywords next to a $ref that do not constrain anything.
_ANNOTATIONS = frozenset({"description", "summary", "title", "example", "examples", "deprecated"})
# Keywords whose values are data, not schemas.
_DATA_KEYWORDS = frozenset({"enum", "const", "default", "example", "examples"})
_SCHEMA_MAPS = ("properties", "patternProperties", "$defs", "dependentSchemas")
_SCHEMA_VALUES = (
    "items",
    "additionalProperties",
    "unevaluatedProperties",
    "unevaluatedItems",
    "contains",
    "propertyNames",
    "not",
    "if",
    "then",
    "else",
)
_UNEXPECTED = re.compile(r"\((.+) (?:was|were) unexpected\)$")
_EMBEDDED = re.compile(r"^([a-z][a-z0-9-]*\.v[0-9]+)(?:#/\$defs/([A-Za-z][A-Za-z0-9_]*))?$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DATE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


class ContractViolation(AssertionError):
    """A request or response that contradicts the contract."""


def contract_path(service: str) -> Path:
    """The contract of ``service`` in the repository (or the schema directory
    of a container image)."""
    if not re.fullmatch(r"[a-z0-9-]+", service):
        raise ValueError(f"invalid service name {service!r}")
    return schema_root() / "contracts" / "openapi" / f"{service}.openapi.yaml"


# ---------------------------------------------------------------- formats

FORMATS = FormatChecker(formats=())


@FORMATS.checks("uuid")
def _is_uuid(value: object) -> bool:
    # The canonical lower-case, hyphenated form is what every service writes.
    return not isinstance(value, str) or _UUID.match(value) is not None


@FORMATS.checks("date-time", raises=ValueError)
def _is_date_time(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if not _DATE_TIME.match(value):
        return False
    datetime.fromisoformat(value)  # rejects impossible dates and times
    return True


@FORMATS.checks("uri")
def _is_uri(value: object) -> bool:
    if not isinstance(value, str):
        return True
    parts = urlsplit(value)
    return bool(parts.scheme and parts.netloc)


def embedded_validator(name: str) -> Draft202012Validator:
    """The canonical validator an ``x-agenttwin-schema`` value names: a
    document schema (``scenario.v1``) or one of its definitions
    (``scenario.v1#/$defs/fault``)."""
    m = _EMBEDDED.match(name)
    if not m:
        raise ValueError(f"invalid x-agenttwin-schema {name!r}")
    document, definition = m.groups()
    return document_definition_validator(document, definition) if definition else document_validator(document)


def _embedded(
    validator: Validator, name: Any, instance: Any, schema: Mapping[str, Any]
) -> Iterator[ValidationError]:
    """``x-agenttwin-schema``: the instance is a document of a canonical schema
    (or a part of one)."""
    document, _, definition = str(name).partition("#/$defs/")
    what = f"{document} {definition}" if definition else f"{document} document"
    for err in embedded_validator(str(name)).iter_errors(instance):
        where = "/".join(str(p) for p in err.absolute_path) or "(root)"
        yield ValidationError(f"is not a valid {what}: {where}: {err.message}")


_ContractValidator: Any = extend(  # type: ignore[no-untyped-call]
    Draft202012Validator, {"x-agenttwin-schema": _embedded}
)


# ---------------------------------------------------------------- schemas


def strictify(schema: Any, *, member: bool = False) -> Any:
    """A copy of ``schema`` in which objects that declare properties reject
    undeclared ones (``unevaluatedProperties: false``), unless the schema says
    otherwise. The members of an ``allOf`` are exempt at their top level — the
    composition as a whole is closed instead, so properties of every member
    count as declared."""
    if not isinstance(schema, Mapping):
        return schema
    out = dict(schema)
    for key in _SCHEMA_MAPS:
        if isinstance(out.get(key), Mapping):
            out[key] = {name: strictify(sub) for name, sub in out[key].items()}
    for key in _SCHEMA_VALUES:
        if isinstance(out.get(key), Mapping):
            out[key] = strictify(out[key])
    if isinstance(out.get("prefixItems"), list):
        out["prefixItems"] = [strictify(sub) for sub in out["prefixItems"]]
    for key in ("anyOf", "oneOf"):
        if isinstance(out.get(key), list):
            out[key] = [strictify(sub) for sub in out[key]]
    if isinstance(out.get("allOf"), list):
        out["allOf"] = [strictify(sub, member=True) for sub in out["allOf"]]
    declares = "properties" in out or "allOf" in out
    if declares and not member and "additionalProperties" not in out and "unevaluatedProperties" not in out:
        out["unevaluatedProperties"] = False
    return out


class _Resolver:
    """Local references (``#/...``) of one document."""

    def __init__(self, document: Mapping[str, Any]) -> None:
        self.document = document

    def pointer(self, ref: str) -> Any:
        if not ref.startswith("#/"):
            raise ValueError(f"only local references are supported, not {ref!r}")
        node: Any = self.document
        for part in ref[2:].split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
        return node

    def follow(self, node: Any) -> Any:
        """The object a (chain of) ``$ref`` points to (responses, parameters)."""
        seen: set[str] = set()
        while isinstance(node, Mapping) and isinstance(node.get("$ref"), str):
            ref = node["$ref"]
            if ref in seen:
                raise ValueError(f"cyclic $ref {ref!r}")
            seen.add(ref)
            node = self.pointer(ref)
        return node

    def inline(self, node: Any, stack: tuple[str, ...] = ()) -> Any:
        """``node`` with every schema reference replaced by its target."""
        if isinstance(node, list):
            return [self.inline(item, stack) for item in node]
        if not isinstance(node, Mapping):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str):
            if ref in stack:
                raise ValueError(f"cyclic $ref {ref!r}")
            target = self.inline(self.pointer(ref), (*stack, ref))
            siblings = {k: v for k, v in node.items() if k != "$ref" and k not in _ANNOTATIONS}
            if not siblings:
                return target
            return {"allOf": [target], **{k: self.inline(v, stack) for k, v in siblings.items()}}
        return {k: (v if k in _DATA_KEYWORDS else self.inline(v, stack)) for k, v in node.items()}


# ---------------------------------------------------------------- the contract


@dataclass(frozen=True)
class Operation:
    method: str  # upper case; "POST" for a webhook
    path: str  # the path template, or "webhook:<name>"
    operation_id: str
    definition: Mapping[str, Any]
    parameters: tuple[Mapping[str, Any], ...]

    @property
    def segments(self) -> tuple[str, ...]:
        return _segments(self.path)


def _segments(path: str) -> tuple[str, ...]:
    """Exact segments: ``/a/`` is not ``/a`` (a trailing slash is a different path)."""
    return tuple(path.split("/")[1:]) if path.startswith("/") else (path,)


def _coerce(raw: str, schema: Mapping[str, Any]) -> Any:
    """A query/path/header value as the type its schema declares."""
    types = schema.get("type")
    kinds = set(types) if isinstance(types, list) else {types}
    try:
        if "integer" in kinds:
            return int(raw)
        if "number" in kinds:
            return float(raw)
    except ValueError:
        return raw
    if "boolean" in kinds and raw in ("true", "false"):
        return raw == "true"
    return raw


def _unexpected(error: ValidationError) -> tuple[str, ...] | None:
    """The property names an ``unevaluatedProperties`` error reports."""
    m = _UNEXPECTED.search(error.message)
    if error.validator != "unevaluatedProperties" or not m:
        return None
    try:
        names = ast.literal_eval(f"({m.group(1)},)")
    except (ValueError, SyntaxError):
        return None
    return tuple(str(n) for n in names)


def _problems(errors: Iterable[ValidationError]) -> list[str]:
    errors = list(errors)

    def echo(e: ValidationError) -> bool:
        # A property that fails its own schema is not "evaluated", so it is
        # also reported as unexpected by its object: keep only the cause.
        names = _unexpected(e)
        base = list(e.absolute_path)
        return bool(names) and all(
            any(list(o.absolute_path)[: len(base) + 1] == [*base, n] for o in errors if o is not e)
            for n in names or ()
        )

    ordered = sorted((e for e in errors if not echo(e)), key=lambda e: [str(p) for p in e.absolute_path])
    return [f"  at /{'/'.join(str(p) for p in e.absolute_path)}: {e.message}" for e in ordered[:10]]


class Contract:
    def __init__(self, document: Mapping[str, Any], name: str = "contract") -> None:
        self.document = document
        self.name = name
        self._resolver = _Resolver(document)
        self.operations: dict[str, Operation] = {}
        for path, item in (document.get("paths") or {}).items():
            self._add_item(path, self._resolver.follow(item))
        for hook, item in (document.get("webhooks") or {}).items():
            self._add_item(f"webhook:{hook}", self._resolver.follow(item))
        self.seen: dict[str, set[int]] = defaultdict(set)
        # Violations found where raising does not reach the test (a worker
        # catches everything its agent call raises); the test asserts none.
        self.violations: list[str] = []
        self._validators: dict[tuple[str, ...], Validator] = {}

    @classmethod
    def load(cls, path: Path) -> Contract:
        document = load_yaml(path.read_text(encoding="utf-8"), max_bytes=4 << 20, max_nodes=200_000)
        if not isinstance(document, dict) or not str(document.get("openapi", "")).startswith("3.1"):
            raise ValueError(f"{path} is not an OpenAPI 3.1 document")
        return cls(document, path.name.split(".")[0])

    def _add_item(self, path: str, item: Mapping[str, Any]) -> None:
        shared = [self._resolver.follow(p) for p in item.get("parameters") or []]
        for method in METHODS:
            op = item.get(method)
            if not isinstance(op, Mapping):
                continue
            own = [self._resolver.follow(p) for p in op.get("parameters") or []]
            names = {(p["in"], p["name"].lower()) for p in own}
            params = tuple(own + [p for p in shared if (p["in"], p["name"].lower()) not in names])
            op_id = str(op.get("operationId") or f"{method.upper()} {path}")
            if op_id in self.operations:
                raise ValueError(f"duplicate operationId {op_id!r}")
            self.operations[op_id] = Operation(method.upper(), path, op_id, op, params)

    # -- lookup ---------------------------------------------------------------

    def pointer(self, ref: str) -> Any:
        """The target of a local reference (``#/components/...``)."""
        return self._resolver.pointer(ref)

    def follow(self, node: Any) -> Any:
        """``node``, or what its (chain of) ``$ref`` points to."""
        return self._resolver.follow(node)

    def find(self, method: str, path: str) -> tuple[Operation, dict[str, str]] | None:
        """The operation for a concrete path, and its path parameters. Literal
        segments win over templated ones (``/simulations/capabilities`` is not
        a run id)."""
        segments = _segments(path)
        best: tuple[int, Operation, dict[str, str]] | None = None
        for op in self.operations.values():
            if op.method != method.upper() or op.path.startswith("webhook:"):
                continue
            pattern = op.segments
            if len(pattern) != len(segments):
                continue
            values: dict[str, str] = {}
            for want, got in zip(pattern, segments, strict=True):
                if want.startswith("{") and want.endswith("}"):
                    if not got:
                        break
                    values[want[1:-1]] = got
                elif want != got:
                    break
            else:
                literal = len(pattern) - len(values)
                if best is None or literal > best[0]:
                    best = (literal, op, values)
        return (best[1], best[2]) if best else None

    def _operation(self, method: str, path: str) -> tuple[Operation, dict[str, str]]:
        found = self.find(method, path)
        if found is None:
            raise ContractViolation(f"{self.name}: {method.upper()} {path} is not a documented operation")
        return found

    def _webhook(self, name: str) -> Operation:
        for op in self.operations.values():
            if op.path == f"webhook:{name}":
                return op
        raise ContractViolation(f"{self.name}: there is no webhook {name!r}")

    def response_for(self, op: Operation, status: int) -> tuple[str, Mapping[str, Any]] | None:
        responses = op.definition.get("responses") or {}
        for key in (str(status), f"{str(status)[0]}XX", "default"):
            if key in responses:
                return key, self._resolver.follow(responses[key])
        return None

    def _validator(self, key: tuple[str, ...], schema: Mapping[str, Any]) -> Validator:
        validator = self._validators.get(key)
        if validator is None:
            strict = strictify(self._resolver.inline(schema))
            validator = _ContractValidator(strict, format_checker=FORMATS)
            self._validators[key] = validator
        return validator

    # -- checks ---------------------------------------------------------------

    def _check_body(
        self,
        where: str,
        content: Mapping[str, Any] | None,
        content_type: str | None,
        body: bytes,
        key: tuple[str, ...],
    ) -> None:
        if not content:
            if body.strip():
                raise ContractViolation(f"{where}: no body is documented, but one was sent")
            return
        media = (content_type or "").split(";")[0].strip().lower()
        if media not in content:
            raise ContractViolation(
                f"{where}: content type {media or '(none)'} is not documented ({', '.join(content)})"
            )
        schema = content[media].get("schema")
        if not schema:
            return  # any content (e.g. a twin's reply)
        if media != "application/json" and not media.endswith("+json"):
            return
        try:
            instance = json.loads(body)
        except ValueError as err:
            raise ContractViolation(f"{where}: the body is not JSON ({err})") from None
        problems = _problems(self._validator(key, schema).iter_errors(instance))
        if problems:
            raise ContractViolation(f"{where} does not match the contract:\n" + "\n".join(problems))

    def check_schema(self, name: str, instance: Any) -> None:
        """Checks a value against a component schema (``#/components/schemas/<name>``)."""
        schema = {"$ref": f"#/components/schemas/{name.replace('~', '~0').replace('/', '~1')}"}
        problems = _problems(self._validator(("schema", name), schema).iter_errors(instance))
        if problems:
            raise ContractViolation(f"{self.name}: not a valid {name}:\n" + "\n".join(problems))

    def check_response(
        self, method: str, path: str, status: int, content_type: str | None, body: bytes
    ) -> Operation:
        """Checks one response; records it for :meth:`uncovered`."""
        op, _ = self._operation(method, path)
        self._check_response_of(op, status, content_type, body)
        self.seen[op.operation_id].add(status)
        return op

    def _check_response_of(self, op: Operation, status: int, content_type: str | None, body: bytes) -> None:
        matched = self.response_for(op, status)
        if matched is None:
            raise ContractViolation(
                f"{self.name}: {op.operation_id} answered {status}, which is not documented"
            )
        key, response = matched
        self._check_body(
            f"{self.name}: {op.operation_id} {status} response",
            response.get("content"),
            content_type,
            body,
            (op.operation_id, "response", key),
        )

    def _check_parameters(
        self,
        op: Operation,
        location: str,
        given: Mapping[str, Sequence[str]],
        *,
        allow_undocumented: bool = False,
    ) -> None:
        documented = {p["name"].lower(): p for p in op.parameters if p["in"] == location}
        for name, values in given.items():
            param = documented.get(name.lower())
            if param is None:
                if not allow_undocumented:
                    raise ContractViolation(
                        f"{self.name}: {op.operation_id} was called with the undocumented "
                        f"{location} parameter {name!r}"
                    )
                continue
            schema = param.get("schema") or {}
            validator = self._validator((op.operation_id, location, name.lower()), schema)
            for raw in values:
                problems = _problems(validator.iter_errors(_coerce(raw, self._resolver.inline(schema))))
                if problems:
                    raise ContractViolation(
                        f"{self.name}: {op.operation_id} {location} parameter {name!r}={raw!r}:\n"
                        + "\n".join(problems)
                    )
        for name, param in documented.items():
            if param.get("required") and name not in {n.lower() for n in given}:
                raise ContractViolation(
                    f"{self.name}: {op.operation_id} requires the {location} parameter {param['name']!r}"
                )

    def check_request(
        self,
        method: str,
        path: str,
        *,
        query: Iterable[tuple[str, str]] = (),
        headers: Mapping[str, str] | None = None,
        content_type: str | None = None,
        body: bytes = b"",
    ) -> Operation:
        """Checks a request the server accepted: its parameters and body are
        what the contract documents."""
        op, path_values = self._operation(method, path)
        self._check_request_of(op, path_values, query, headers or {}, content_type, body)
        return op

    def _check_request_of(
        self,
        op: Operation,
        path_values: Mapping[str, str],
        query: Iterable[tuple[str, str]],
        headers: Mapping[str, str],
        content_type: str | None,
        body: bytes,
    ) -> None:
        self._check_parameters(op, "path", {k: [v] for k, v in path_values.items()})
        grouped: dict[str, list[str]] = defaultdict(list)
        for k, v in query:
            grouped[k].append(v)
        self._check_parameters(op, "query", grouped)
        documented_headers = {p["name"].lower() for p in op.parameters if p["in"] == "header"}
        present = {k.lower(): [v] for k, v in headers.items() if k.lower() in documented_headers}
        self._check_parameters(op, "header", present)
        request_body = self._resolver.follow(op.definition.get("requestBody") or {})
        if not request_body:
            if body.strip():
                raise ContractViolation(f"{self.name}: {op.operation_id} documents no request body")
            return
        if not body.strip():
            if request_body.get("required"):
                raise ContractViolation(f"{self.name}: {op.operation_id} requires a request body")
            return
        self._check_body(
            f"{self.name}: {op.operation_id} request",
            request_body.get("content"),
            content_type,
            body,
            (op.operation_id, "request"),
        )

    def check_exchange(self, request: httpx.Request, response: httpx.Response) -> Operation:
        """Checks a response (read already) and — when the server accepted the
        request (2xx) — the request as well."""
        op, path_values = self._operation(request.method, request.url.path)
        self._check_response_of(
            op, response.status_code, response.headers.get("content-type"), response.content
        )
        if 200 <= response.status_code < 300:
            self._check_request_of(
                op,
                path_values,
                request.url.params.multi_items(),
                request.headers,
                request.headers.get("content-type"),
                request.content,
            )
        self.seen[op.operation_id].add(response.status_code)
        return op

    def check_webhook(self, name: str, request: httpx.Request, response: httpx.Response | None) -> None:
        """Checks a call the service made to a webhook: always the request, the
        response when there is one."""
        op = self._webhook(name)
        self._check_request_of(op, {}, (), {}, request.headers.get("content-type"), request.content)
        if response is not None:
            self._check_response_of(
                op, response.status_code, response.headers.get("content-type"), response.content
            )
            self.seen[op.operation_id].add(response.status_code)

    def response_hook(self) -> Callable[[httpx.Response], Awaitable[None]]:
        """An ``httpx.AsyncClient`` response event hook checking every exchange."""

        async def hook(response: httpx.Response) -> None:
            await response.aread()
            self.check_exchange(response.request, response)

        return hook

    def uncovered(self) -> list[str]:
        """Operations without a successful (2xx) exchange that passed its check."""
        return sorted(
            op_id
            for op_id in self.operations
            if not any(200 <= status < 300 for status in self.seen.get(op_id, ()))
        )


class ContractTransport(httpx.AsyncBaseTransport):
    """Checks the calls a service makes to one of the contract's webhooks (for
    example the agent adapter contract) on their way through."""

    def __init__(
        self, contract: Contract, webhook: str, inner: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.contract = contract
        self.webhook = webhook
        self.inner = inner or httpx.AsyncHTTPTransport()

    def _check(self, request: httpx.Request, response: httpx.Response | None) -> None:
        try:
            self.contract.check_webhook(self.webhook, request, response)
        except ContractViolation as err:
            self.contract.violations.append(str(err))
            raise

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        try:
            response = await self.inner.handle_async_request(request)
        except httpx.HTTPError:
            self._check(request, None)
            raise
        content = await response.aread()
        await response.aclose()
        checked = httpx.Response(
            response.status_code,
            headers=response.headers,
            content=content,
            request=request,
            extensions=response.extensions,
        )
        self._check(request, checked)
        return checked

    async def aclose(self) -> None:
        await self.inner.aclose()
