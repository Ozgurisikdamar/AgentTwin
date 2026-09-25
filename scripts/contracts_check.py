"""Detects breaking changes in the published contracts: the event schemas and
the OpenAPI documents of the HTTP APIs.

The rules are in ``packages/contracts/README.md``. Compared with the committed
baselines (``packages/contracts/events/.baseline.json`` and
``packages/contracts/openapi/.baseline.json``):

Events (within an event version only additive changes are allowed) — breaking
when a published schema disappears, a required field is removed or made
optional, a field that already existed becomes required (old producers do not
send it), the JSON type of a field changes or an allowed enum value is removed.

HTTP APIs, per operation —
* requests (clients may send what was documented): an operation disappears, a
  parameter or body field is removed (servers reject unknown fields), a new
  parameter or field is required, a type changes (other than taking more
  types, e.g. also ``null``) or an allowed enum value is removed;
* responses (clients may rely on what was documented): a success status
  disappears, a required field is removed or made optional, a type changes
  (other than sending fewer types, e.g. no longer ``null``) or an embedded
  document (``x-agenttwin-schema``) changes its schema. New fields and new
  enum values are compatible: clients ignore what they do not know.
Webhooks reverse the roles: the service sends the request (response rules) and
reads the answer (request rules).

New optional fields, new operations and new event types are compatible.

    python scripts/contracts_check.py            # check (exit 1 on breaking changes)
    python scripts/contracts_check.py --update   # record the current contracts as published
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from agenttwin_core.yamlsafe import load_yaml

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ROOT / "packages" / "contracts" / "events"
BASELINE = EVENTS / ".baseline.json"
OPENAPI = ROOT / "packages" / "contracts" / "openapi"
OPENAPI_BASELINE = OPENAPI / ".baseline.json"
# Every operation an OpenAPI 3.1 path item can hold: one left out here would
# be left out of the baseline, and could then change without being noticed.
METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

Fingerprint = dict[str, dict[str, list[str]]]


def _resolve(node: Any, root: dict[str, Any], seen: frozenset[str]) -> tuple[Any, frozenset[str]]:
    while isinstance(node, dict) and isinstance(node.get("$ref"), str):
        ref = node["$ref"]
        if not ref.startswith("#/") or ref in seen:
            raise ValueError(f"unsupported or cyclic $ref {ref!r}")
        seen = seen | {ref}
        target: Any = root
        for part in ref[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        node = target
    return node, seen


def _normalize(node: Any, root: dict[str, Any], seen: frozenset[str]) -> tuple[Any, frozenset[str]]:
    """The schema with references followed, ``allOf`` members merged and
    ``anyOf: [X, {type: null}]`` read as a nullable X — so documents that say
    the same thing in different shapes have the same fingerprint."""
    node, seen = _resolve(node, root, seen)
    if not isinstance(node, dict):
        return node, seen
    variants = node.get("anyOf") or node.get("oneOf")
    if isinstance(variants, list) and len(variants) == 2:
        nulls = [v for v in variants if isinstance(v, dict) and v.get("type") == "null"]
        if len(nulls) == 1:
            other, seen = _normalize(next(v for v in variants if v is not nulls[0]), root, seen)
            if isinstance(other, dict):
                merged = {k: v for k, v in other.items()}
                t = other.get("type")
                types = set(t) if isinstance(t, list) else ({t} if t else set())
                if types:
                    merged["type"] = sorted(types | {"null"})
                return merged, seen
    members = node.get("allOf")
    if isinstance(members, list):
        merged = {k: v for k, v in node.items() if k != "allOf"}
        props: dict[str, Any] = {}
        required: set[str] = set(node.get("required") or [])
        for member in members:
            m, seen = _normalize(member, root, seen)
            if not isinstance(m, dict):
                continue
            props.update(m.get("properties") or {})
            required |= set(m.get("required") or [])
            for key in ("type", "enum", "items"):
                if key in m and key not in merged:
                    merged[key] = m[key]
        props.update(node.get("properties") or {})
        if props:
            merged["properties"] = props
        if required:
            merged["required"] = sorted(required)
        return merged, seen
    return node, seen


def fingerprint(schema: Any, root: dict[str, Any] | None = None) -> Fingerprint:
    """The compatibility-relevant facts of a schema, keyed by field path."""
    doc = root if root is not None else schema
    out: Fingerprint = {"types": {}, "required": {}, "enums": {}}

    def walk(node: Any, path: str, seen: frozenset[str]) -> None:
        node, seen = _normalize(node, doc, seen)
        if not isinstance(node, dict):
            return
        key = path or "/"
        t = node.get("type")
        if t is not None:
            out["types"][key] = sorted(t) if isinstance(t, list) else [t]
        if isinstance(node.get("enum"), list):
            out["enums"][key] = sorted(json.dumps(v, sort_keys=True) for v in node["enum"])
        if node.get("required"):
            out["required"][key] = sorted(node["required"])
        if isinstance(node.get("x-agenttwin-schema"), str):
            out.setdefault("embedded", {})[key] = [node["x-agenttwin-schema"]]
        for name, sub in (node.get("properties") or {}).items():
            walk(sub, f"{path}/{name}", seen)
        if isinstance(node.get("items"), dict):
            walk(node["items"], f"{path}/[]", seen)
        for combinator in ("anyOf", "oneOf"):
            for i, sub in enumerate(node.get(combinator) or []):
                walk(sub, f"{path}/{combinator}[{i}]", seen)

    walk(schema, "", frozenset())
    return out


def current() -> dict[str, Fingerprint]:
    prints = {}
    for path in sorted(EVENTS.glob("*.schema.json")):
        name = path.name.removesuffix(".schema.json")
        prints[name] = fingerprint(json.loads(path.read_text(encoding="utf-8")))
    return prints


def _join(path: str, field: str) -> str:
    return f"{path.rstrip('/')}/{field}"


def _removed_or_optional(where: str, old: Fingerprint, new: Fingerprint) -> list[str]:
    problems = []
    for path, fields in old["required"].items():
        for f in fields:
            if f not in new["required"].get(path, []):
                problems.append(f"{where}: required field {_join(path, f)} was removed or made optional")
    return problems


def _type_changes(where: str, old: Fingerprint, new: Fingerprint, *, allow: str = "") -> list[str]:
    """Fields whose JSON types changed. ``allow="narrower"`` accepts fewer
    types (what a client receives: a response that stops sending ``null``
    still only sends what was documented); ``allow="wider"`` accepts more
    (what a client sends: a request that also takes ``null`` still takes what
    was documented). An event has readers and writers, so any change counts."""

    def compatible(before: list[str], after: list[str]) -> bool:
        if allow == "narrower":
            return set(after) <= set(before)
        if allow == "wider":
            return set(after) >= set(before)
        return after == before

    return [
        f"{where}: type of {path} changed from {types} to {new['types'][path]}"
        for path, types in old["types"].items()
        if path in new["types"] and not compatible(types, new["types"][path])
    ]


def _enum_removals(where: str, old: Fingerprint, new: Fingerprint) -> list[str]:
    problems = []
    for path, values in old["enums"].items():
        removed = sorted(set(values) - set(new["enums"].get(path, values)))
        if removed:
            problems.append(f"{where}: enum values removed at {path}: {', '.join(removed)}")
    return problems


def breaking_changes(baseline: dict[str, Fingerprint], now: dict[str, Fingerprint]) -> list[str]:
    problems: list[str] = []
    for event, old in sorted(baseline.items()):
        new = now.get(event)
        if new is None:
            problems.append(f"{event}: published schema was removed (publish a new version instead)")
            continue
        problems += _removed_or_optional(event, old, new)
        for path, fields in new["required"].items():
            if path not in old["types"] and path not in old["required"]:
                continue  # a new (optional) object may have its own required fields
            for f in fields:
                if f not in old["required"].get(path, []) and _join(path, f) in old["types"]:
                    problems.append(f"{event}: existing field {_join(path, f)} became required")
                elif f not in old["required"].get(path, []):
                    problems.append(f"{event}: new required field {_join(path, f)} (add it as optional)")
        problems += _type_changes(event, old, new)
        problems += _enum_removals(event, old, new)
    return problems


# ---------------------------------------------------------------- OpenAPI


def _body(content: Any, root: dict[str, Any]) -> Fingerprint | None:
    if not isinstance(content, dict):
        return None
    media = content.get("application/json")
    if not isinstance(media, dict) or not media.get("schema"):
        return None
    return fingerprint(media["schema"], root)


def openapi_fingerprint(doc: dict[str, Any]) -> dict[str, Any]:
    """Per operation (``METHOD path``, or ``WEBHOOK name`` for webhooks): its
    parameters, request body and success responses."""
    out: dict[str, Any] = {}

    def operation(key: str, item: Any, webhook: bool) -> None:
        item, _ = _resolve(item, doc, frozenset())
        shared = item.get("parameters") or []
        for method in METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            params: dict[str, Any] = {}
            for raw in [*shared, *(op.get("parameters") or [])]:
                p, _ = _resolve(raw, doc, frozenset())
                params[f"{p['in']}:{p['name'].lower() if p['in'] == 'header' else p['name']}"] = {
                    "required": bool(p.get("required")),
                    "schema": fingerprint(p.get("schema") or {}, doc),
                }
            body, _ = _resolve(op.get("requestBody") or {}, doc, frozenset())
            request = _body(body.get("content"), doc)
            responses = {}
            for status, raw in (op.get("responses") or {}).items():
                if str(status).startswith("2"):
                    response, _ = _resolve(raw, doc, frozenset())
                    responses[str(status)] = _body(response.get("content"), doc)
            name = f"WEBHOOK {key}" if webhook else f"{method.upper()} {key}"
            out[name] = {
                "parameters": params,
                "request": None
                if request is None
                else {"required": bool(body.get("required")), "schema": request},
                "responses": responses,
            }

    for path, item in (doc.get("paths") or {}).items():
        operation(path, item, webhook=False)
    for hook, item in (doc.get("webhooks") or {}).items():
        operation(hook, item, webhook=True)
    return out


def current_openapi() -> dict[str, dict[str, Any]]:
    return {
        path.name.removesuffix(".openapi.yaml"): openapi_fingerprint(
            load_yaml(path.read_text(encoding="utf-8"), max_bytes=4 << 20, max_nodes=200_000)
        )
        for path in sorted(OPENAPI.glob("*.openapi.yaml"))
    }


def _request_changes(where: str, old: Fingerprint, new: Fingerprint, *, strict: bool = True) -> list[str]:
    """What a client that followed the old document may no longer send. A
    strict reader (a service rejecting unknown fields) also breaks when a
    field it accepted disappears."""
    problems = [
        f"{where}: field {path} was removed (requests that send it are rejected)"
        for path in old["types"]
        if strict and path not in new["types"] and path != "/"
    ]
    for path, fields in new["required"].items():
        for f in fields:
            if f not in old["required"].get(path, []) and (path in old["types"] or path == "/"):
                problems.append(f"{where}: {_join(path, f)} became required")
    return (
        problems
        + _type_changes(where, old, new, allow="wider")
        + _enum_removals(where, old, new)
        + _embedded_changes(where, old, new, reader=False)
    )


def _embedded_changes(where: str, old: Fingerprint, new: Fingerprint, *, reader: bool) -> list[str]:
    """A field documented as a canonical document (``x-agenttwin-schema``)
    that changes its schema: a reader may no longer rely on the one it knew; a
    writer may no longer send what was accepted when the service starts
    demanding one."""
    before, after = old.get("embedded", {}), new.get("embedded", {})
    paths = before if reader else after
    return [
        f"{where}: {path} changed from {(before.get(path) or ['any value'])[0]} "
        f"to {(after.get(path) or ['any value'])[0]}"
        for path in sorted(paths)
        if before.get(path) != after.get(path)
    ]


def _response_changes(where: str, old: Fingerprint, new: Fingerprint) -> list[str]:
    """What a client that followed the old document may no longer receive."""
    return (
        _removed_or_optional(where, old, new)
        + _type_changes(where, old, new, allow="narrower")
        + _embedded_changes(where, old, new, reader=True)
    )


def openapi_breaking_changes(
    baseline: dict[str, dict[str, Any]], now: dict[str, dict[str, Any]]
) -> list[str]:
    problems: list[str] = []
    for api, old_ops in sorted(baseline.items()):
        new_ops = now.get(api)
        if new_ops is None:
            problems.append(f"{api}: the published API document was removed")
            continue
        for key, old in sorted(old_ops.items()):
            where = f"{api} {key}"
            new = new_ops.get(key)
            if new is None:
                problems.append(f"{where}: the operation was removed")
                continue
            webhook = key.startswith("WEBHOOK ")
            # A webhook's request is sent by the service (response rules) and
            # its answer is read by the service, which ignores unknown fields.
            sends: Any = _response_changes if webhook else _request_changes
            reads: Any = (
                (lambda w, o, n: _request_changes(w, o, n, strict=False)) if webhook else _response_changes
            )
            for name, p in old["parameters"].items():
                if name not in new["parameters"]:
                    problems.append(f"{where}: parameter {name} was removed")
                else:
                    problems += sends(
                        f"{where} parameter {name}", p["schema"], new["parameters"][name]["schema"]
                    )
            for name, p in new["parameters"].items():
                if p["required"] and not old["parameters"].get(name, {}).get("required"):
                    problems.append(f"{where}: parameter {name} is now required")
            if old["request"] is not None:
                if new["request"] is None:
                    problems.append(f"{where}: the request body was removed")
                else:
                    problems += sends(f"{where} request", old["request"]["schema"], new["request"]["schema"])
            if (
                new["request"] is not None
                and new["request"]["required"]
                and not (old["request"] or {}).get("required")
            ):
                problems.append(f"{where}: a request body is now required")
            for status, schema in old["responses"].items():
                if status not in new["responses"]:
                    problems.append(f"{where}: success response {status} was removed")
                elif schema is not None and new["responses"][status] is not None:
                    problems += reads(f"{where} {status} response", schema, new["responses"][status])
                elif schema is not None:
                    problems.append(f"{where}: the {status} response lost its body")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--update", action="store_true", help="record the current contracts as the baselines")
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument("--openapi-baseline", type=Path, default=OPENAPI_BASELINE)
    args = parser.parse_args(argv)

    now = current()
    baseline: dict[str, Fingerprint] = {}
    if args.baseline.exists():
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    apis = current_openapi()
    api_baseline: dict[str, dict[str, Any]] = {}
    if args.openapi_baseline.exists():
        api_baseline = json.loads(args.openapi_baseline.read_text(encoding="utf-8"))

    problems = breaking_changes(baseline, now) + openapi_breaking_changes(api_baseline, apis)
    for p in problems:
        print(f"BREAKING {p}")
    if problems:
        print(f"{len(problems)} breaking change(s); see packages/contracts/README.md")
        return 1
    operations = sum(len(ops) for ops in apis.values())
    if args.update:
        args.baseline.write_text(json.dumps(now, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        args.openapi_baseline.write_text(json.dumps(apis, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"baselines updated: {len(now)} event schemas, ", end="")
        print(f"{len(apis)} API documents ({operations} operations)")
        return 0
    added = sorted(set(now) - set(baseline))
    print(
        f"{len(now)} event schemas compatible with the baseline"
        + (f" (new: {', '.join(added)})" if added else "")
    )
    new = sorted(api for api in apis if api not in api_baseline) + sorted(
        f"{api} {key}"
        for api, ops in apis.items()
        if api in api_baseline
        for key in ops
        if key not in api_baseline[api]
    )
    print(
        f"{len(apis)} API documents ({operations} operations) compatible with the baseline"
        + (f" (new: {', '.join(new)})" if new else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
