"""Comparators shared by tool-twin conditions and scenario expectations:
``equals``, ``notEquals``, ``in``, ``exists``, ``gte``, ``lte``, ``gt``,
``lt`` and ``matches``. Several comparators in one spec must all hold.

``matches`` uses the ``regex`` engine with a timeout, so a pathological
pattern in a scenario cannot hang a worker (spec §112).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import regex

from agenttwin_core.paths import MISSING, json_equal

__all__ = ["COMPARATORS", "Comparison", "RegexTimeout", "compare", "search"]

COMPARATORS = ("equals", "notEquals", "in", "exists", "gte", "lte", "gt", "lt", "matches")
REGEX_TIMEOUT_S = 0.5
MAX_REGEX_INPUT = 1 << 20


class RegexTimeout(Exception):
    """A pattern took longer than the allowed time (reported as ERROR)."""


def search(pattern: str, text: str, *, ignore_case: bool = False) -> regex.Match[str] | None:
    flags = regex.IGNORECASE if ignore_case else 0
    try:
        return regex.search(pattern, text[:MAX_REGEX_INPUT], flags=flags, timeout=REGEX_TIMEOUT_S)
    except TimeoutError:
        raise RegexTimeout(f"pattern {pattern[:80]!r} timed out") from None


def _num(v: Any) -> float | None:
    if isinstance(v, int | float) and not isinstance(v, bool):
        return float(v)
    return None


@dataclass(frozen=True)
class Comparison:
    ok: bool
    reason: str


def _short(v: Any) -> str:
    text = "missing" if v is MISSING else repr(v)
    return text if len(text) <= 120 else text[:117] + "..."


def compare(actual: Any, spec: Mapping[str, Any]) -> Comparison:
    """Evaluates every comparator present in ``spec`` against ``actual``.
    ``actual`` may be :data:`MISSING`. With no comparator the value must exist."""
    present = [k for k in COMPARATORS if k in spec]
    if not present:
        present = ["exists"]
        spec = {"exists": True}
    for key in present:
        expected = spec[key]
        if key == "exists":
            has = actual is not MISSING and actual is not None
            if bool(expected) != has:
                return Comparison(False, f"expected the value to {'exist' if expected else 'be absent'}")
        elif key == "equals":
            if actual is MISSING or not json_equal(actual, expected):
                return Comparison(False, f"expected {_short(expected)}, got {_short(actual)}")
        elif key == "notEquals":
            if actual is not MISSING and json_equal(actual, expected):
                return Comparison(False, f"expected anything but {_short(expected)}")
        elif key == "in":
            options = expected if isinstance(expected, list) else [expected]
            if actual is MISSING or not any(json_equal(actual, o) for o in options):
                return Comparison(False, f"expected one of {_short(options)}, got {_short(actual)}")
        elif key in ("gte", "lte", "gt", "lt"):
            a, e = _num(actual), _num(expected)
            if a is None or e is None:
                return Comparison(False, f"{key} needs numbers, got {_short(actual)} and {_short(expected)}")
            ok = {"gte": a >= e, "lte": a <= e, "gt": a > e, "lt": a < e}[key]
            if not ok:
                symbol = {"gte": ">=", "lte": "<=", "gt": ">", "lt": "<"}[key]
                return Comparison(False, f"expected {symbol} {e:g}, got {a:g}")
        elif key == "matches":
            if not isinstance(actual, str) or search(str(expected), actual) is None:
                return Comparison(
                    False, f"expected a value matching {str(expected)[:80]!r}, got {_short(actual)}"
                )
    return Comparison(True, "ok")
