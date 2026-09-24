"""Detects breaking changes in the published event schemas.

The compatibility rules are in ``packages/contracts/README.md``: within an event
version only additive changes are allowed. Compared with the committed baseline
(``packages/contracts/events/.baseline.json``) a change is breaking when

* a published event schema disappears,
* a required field is removed or made optional,
* a field that already existed becomes required (old producers do not send it),
* the JSON type of an existing field changes, or
* an allowed enum value is removed.

New optional fields, new nested objects and new event types are compatible.

    python scripts/contracts_check.py            # check (exit 1 on breaking changes)
    python scripts/contracts_check.py --update   # record the current schemas as published
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ROOT / "packages" / "contracts" / "events"
BASELINE = EVENTS / ".baseline.json"

Fingerprint = dict[str, dict[str, list[str]]]


def _resolve(node: Any, root: dict[str, Any], seen: frozenset[str]) -> Any:
    ref = node.get("$ref") if isinstance(node, dict) else None
    if not isinstance(ref, str):
        return node
    if not ref.startswith("#/") or ref in seen:
        raise ValueError(f"unsupported or cyclic $ref {ref!r}")
    target: Any = root
    for part in ref[2:].split("/"):
        target = target[part.replace("~1", "/").replace("~0", "~")]
    return _resolve(target, root, seen | {ref})


def fingerprint(schema: dict[str, Any]) -> Fingerprint:
    """The compatibility-relevant facts of a schema, keyed by field path."""
    out: Fingerprint = {"types": {}, "required": {}, "enums": {}}

    def walk(node: Any, path: str, seen: frozenset[str]) -> None:
        node = _resolve(node, schema, seen)
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
        for name, sub in (node.get("properties") or {}).items():
            walk(sub, f"{path}/{name}", seen)
        if isinstance(node.get("items"), dict):
            walk(node["items"], f"{path}/[]", seen)
        for combinator in ("allOf", "anyOf", "oneOf"):
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


def breaking_changes(baseline: dict[str, Fingerprint], now: dict[str, Fingerprint]) -> list[str]:
    problems: list[str] = []
    for event, old in sorted(baseline.items()):
        new = now.get(event)
        if new is None:
            problems.append(f"{event}: published schema was removed (publish a new version instead)")
            continue
        for path, fields in old["required"].items():
            for f in fields:
                if f not in new["required"].get(path, []):
                    problems.append(
                        f"{event}: required field {path.rstrip('/')}/{f} was removed or made optional"
                    )
        for path, fields in new["required"].items():
            if path not in old["types"] and path not in old["required"]:
                continue  # a new (optional) object may have its own required fields
            for f in fields:
                if f not in old["required"].get(path, []) and f"{path.rstrip('/')}/{f}" in old["types"]:
                    problems.append(f"{event}: existing field {path.rstrip('/')}/{f} became required")
                elif f not in old["required"].get(path, []):
                    problems.append(
                        f"{event}: new required field {path.rstrip('/')}/{f} (add it as optional)"
                    )
        for path, types in old["types"].items():
            if path in new["types"] and new["types"][path] != types:
                problems.append(f"{event}: type of {path} changed from {types} to {new['types'][path]}")
        for path, values in old["enums"].items():
            removed = sorted(set(values) - set(new["enums"].get(path, values)))
            if removed:
                problems.append(f"{event}: enum values removed at {path}: {', '.join(removed)}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", action="store_true", help="record the current schemas as the baseline")
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    args = parser.parse_args(argv)
    now = current()
    baseline: dict[str, Fingerprint] = {}
    if args.baseline.exists():
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    problems = breaking_changes(baseline, now)
    for p in problems:
        print(f"BREAKING {p}")
    if problems:
        print(f"{len(problems)} breaking change(s) in event schemas; see packages/contracts/README.md")
        return 1
    added = sorted(set(now) - set(baseline))
    if args.update:
        args.baseline.write_text(json.dumps(now, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"baseline updated: {len(now)} event schemas")
    else:
        print(
            f"{len(now)} event schemas compatible with the baseline"
            + (f" (new: {', '.join(added)})" if added else "")
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
