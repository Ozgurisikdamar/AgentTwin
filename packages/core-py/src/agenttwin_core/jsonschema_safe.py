"""JSON Schema validation that is safe to run on untrusted schemas and data.

jsonschema evaluates ``pattern`` with Python's ``re``, which has no time
limit: a catastrophic pattern plus a crafted instance could hang a worker.
Here ``pattern`` runs on the ``regex`` engine with a timeout (spec §112).
``patternProperties`` is refused because it is evaluated with ``re`` in
several keywords (``patternProperties``, ``additionalProperties``,
``unevaluatedProperties``) that cannot all be given a time limit. Remote
``$ref`` retrieval is never performed (jsonschema's default registry does
not fetch).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import SchemaError, ValidationError

from agenttwin_core.compare import search

__all__ = ["SafeValidator", "schema_problem", "validation_errors"]


def _safe_pattern(validator: Any, pattern: str, instance: Any, schema: Any) -> Iterator[ValidationError]:
    if validator.is_type(instance, "string") and search(pattern, instance) is None:
        yield ValidationError(f"{instance[:80]!r} does not match {pattern!r}")


SafeValidator: Any = validators.extend(  # type: ignore[no-untyped-call]
    Draft202012Validator, validators={"pattern": _safe_pattern}
)


def _has_key(node: Any, key: str) -> bool:
    if isinstance(node, Mapping):
        return key in node or any(_has_key(v, key) for v in node.values())
    if isinstance(node, list):
        return any(_has_key(v, key) for v in node)
    return False


def schema_problem(schema: Any) -> str | None:
    """Why ``schema`` cannot be used for validation, or None."""
    if not isinstance(schema, Mapping):
        return "schema must be an object"
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as err:
        return f"invalid JSON schema: {err.message}"
    if _has_key(schema, "patternProperties"):
        return "patternProperties is not supported (use propertyNames with pattern)"
    return None


def validation_errors(schema: Mapping[str, Any], instance: Any, limit: int = 10) -> list[str]:
    """Human-readable problems of ``instance`` (``where: message``), first
    ``limit`` in document order. The schema must have passed :func:`schema_problem`.
    May raise :class:`agenttwin_core.compare.RegexTimeout`."""
    errors = sorted(SafeValidator(schema).iter_errors(instance), key=lambda e: [str(p) for p in e.path])
    out = []
    for e in errors[:limit]:
        where = "/".join(str(p) for p in e.path) or "(root)"
        out.append(f"{where}: {e.message}")
    return out
