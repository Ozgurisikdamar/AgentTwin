"""A small, safe path language for JSON-like data.

Used for tool-twin state (``orders.ORD-1001.status``), scenario expectations
and a subset of JSONPath (``$.items[0].id``). There are no filters, wildcards
or script expressions — evaluating a path can never execute code and costs
O(segments).

Grammar::

    path     := ["$"] segment*        (the first segment may omit the dot)
    segment  := "." key | "[" index "]" | "[" quoted "]" | ".length()"
    key      := one or more characters other than . [ ] ' " whitespace
    index    := -?[0-9]+              (negative counts from the end)
    quoted   := 'text' | "text"       (backslash escapes the quote)

``.length()`` yields the length of a list, object or string.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final

__all__ = [
    "LENGTH",
    "MISSING",
    "PathError",
    "Segment",
    "delete_path",
    "diff_state",
    "format_path",
    "get_path",
    "json_equal",
    "parse_path",
    "set_path",
]

MAX_PATH_LENGTH = 500
MAX_SEGMENTS = 64
# diff_state: at most this many changes, each value shown up to this size.
MAX_CHANGES = 100
MAX_DISPLAY = 2000


class _Missing:
    _instance: _Missing | None = None

    def __new__(cls) -> _Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING: Final = _Missing()


@dataclass(frozen=True)
class _Length:
    def __repr__(self) -> str:
        return "length()"


LENGTH: Final = _Length()

Segment = str | int | _Length

_KEY = re.compile(r"[^.\[\]'\"\s]+")


class PathError(ValueError):
    pass


@lru_cache(maxsize=4096)
def parse_path(path: str) -> tuple[Segment, ...]:
    if not isinstance(path, str):
        raise PathError("path must be a string")
    if len(path) > MAX_PATH_LENGTH:
        raise PathError(f"path is longer than {MAX_PATH_LENGTH} characters")
    i = 0
    n = len(path)
    segments: list[Segment] = []
    if path.startswith("$"):
        i = 1
    first = True
    while i < n:
        c = path[i]
        if c == ".":
            if path.startswith(".length()", i) and (i + 9 == n or path[i + 9] in ".["):
                segments.append(LENGTH)
                i += 9
            else:
                m = _KEY.match(path, i + 1)
                if not m:
                    raise PathError(f"expected a key after '.' at position {i}")
                segments.append(m.group(0))
                i = m.end()
        elif c == "[":
            j = i + 1
            if j < n and path[j] in "'\"":
                quote = path[j]
                j += 1
                buf: list[str] = []
                while j < n and path[j] != quote:
                    if path[j] == "\\" and j + 1 < n:
                        j += 1
                    buf.append(path[j])
                    j += 1
                if j >= n or j + 1 >= n or path[j + 1] != "]":
                    raise PathError(f"unterminated quoted key at position {i}")
                segments.append("".join(buf))
                i = j + 2
            else:
                close = path.find("]", j)
                if close < 0:
                    raise PathError(f"unterminated '[' at position {i}")
                token = path[j:close].strip()
                if not re.fullmatch(r"-?[0-9]+", token):
                    raise PathError(f"index must be an integer at position {i}")
                segments.append(int(token))
                i = close + 1
        elif first and not path.startswith("$"):
            m = _KEY.match(path, i)
            if not m:
                raise PathError(f"unexpected character {c!r} at position {i}")
            segments.append(m.group(0))
            i = m.end()
        else:
            raise PathError(f"unexpected character {c!r} at position {i}")
        first = False
        if len(segments) > MAX_SEGMENTS:
            raise PathError(f"path has more than {MAX_SEGMENTS} segments")
    if path and not segments and path != "$":
        raise PathError("empty path")
    return tuple(segments)


def get_path(data: Any, path: str | Sequence[Segment]) -> Any:
    """The value at ``path`` or :data:`MISSING`."""
    segments = parse_path(path) if isinstance(path, str) else tuple(path)
    cur = data
    for seg in segments:
        if seg is LENGTH:
            if isinstance(cur, (list, tuple, Mapping, str)):
                cur = len(cur)
                continue
            return MISSING
        if isinstance(seg, int):
            if isinstance(cur, (list, tuple)) and -len(cur) <= seg < len(cur):
                cur = cur[seg]
                continue
            return MISSING
        if isinstance(cur, Mapping) and seg in cur:
            cur = cur[seg]
            continue
        return MISSING
    return cur


def _segments(path: str | Sequence[Segment]) -> tuple[Segment, ...]:
    return parse_path(path) if isinstance(path, str) else tuple(path)


def set_path(data: dict[str, Any], path: str | Sequence[Segment], value: Any) -> None:
    """Sets ``path`` creating intermediate objects. List indexes must exist."""
    segments = _segments(path)
    if not segments:
        raise PathError("cannot replace the root")
    cur: Any = data
    for i, seg in enumerate(segments):
        last = i == len(segments) - 1
        if seg is LENGTH:
            raise PathError("length() cannot be assigned")
        if isinstance(seg, int):
            if not isinstance(cur, list) or not -len(cur) <= seg < len(cur):
                raise PathError(f"index {seg} is out of range in {path!r}")
            if last:
                cur[seg] = value
                return
            cur = cur[seg]
            continue
        if not isinstance(cur, dict):
            raise PathError(f"{path!r} traverses a non-object value")
        if last:
            cur[seg] = value
            return
        nxt = cur.get(seg)
        if nxt is None:
            nxt = {}
            cur[seg] = nxt
        cur = nxt


def delete_path(data: dict[str, Any], path: str | Sequence[Segment]) -> bool:
    """Removes ``path``; False when it did not exist."""
    segments = _segments(path)
    if not segments:
        raise PathError("cannot delete the root")
    parent = get_path(data, segments[:-1])
    last = segments[-1]
    if isinstance(last, int):
        if isinstance(parent, list) and -len(parent) <= last < len(parent):
            del parent[last]
            return True
        return False
    if isinstance(last, str) and isinstance(parent, dict) and last in parent:
        del parent[last]
        return True
    return False


def _is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def json_equal(a: Any, b: Any) -> bool:
    """Equality with JSON semantics: ``true`` is not ``1``, ``1`` equals ``1.0``."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _is_number(a) and _is_number(b):
        return math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=1e-9) if (a != b) else True
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list | tuple) and isinstance(b, list | tuple):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b, strict=True))
    return type(a) is type(b) and a == b


def format_path(segments: Sequence[Segment]) -> str:
    """The canonical text of a segment list (``orders['ORD 1'].items[0]``);
    ``parse_path(format_path(s)) == s``."""
    out: list[str] = []
    for i, seg in enumerate(segments):
        if seg is LENGTH:
            out.append(".length()")
        elif isinstance(seg, int):
            out.append(f"[{seg}]")
        elif isinstance(seg, str) and _KEY.fullmatch(seg) and seg != "length()" and not seg.startswith("$"):
            out.append(seg if i == 0 else "." + seg)
        else:
            escaped = str(seg).replace("\\", "\\\\").replace("'", "\\'")
            out.append(f"['{escaped}']")
    return "".join(out)


def _display(value: Any) -> Any:
    text = json.dumps(value, sort_keys=True, default=str)
    return value if len(text) <= MAX_DISPLAY else text[: MAX_DISPLAY - 3] + "..."


def diff_state(before: Any, after: Any, *, limit: int = MAX_CHANGES) -> list[dict[str, Any]]:
    """The changes between two states, as ``{op, path, before?, after?}``."""
    out: list[dict[str, Any]] = []

    def walk(a: Any, b: Any, path: tuple[Segment, ...]) -> None:
        if len(out) >= limit:
            return
        if isinstance(a, Mapping) and isinstance(b, Mapping):
            for k in sorted(set(a) | set(b), key=str):
                if len(out) >= limit:
                    return
                if k not in b:
                    out.append({"op": "removed", "path": format_path((*path, k)), "before": _display(a[k])})
                elif k not in a:
                    out.append({"op": "added", "path": format_path((*path, k)), "after": _display(b[k])})
                else:
                    walk(a[k], b[k], (*path, k))
            return
        if isinstance(a, list) and isinstance(b, list):
            if len(b) > len(a) and json_equal(a, b[: len(a)]):
                for i in range(len(a), len(b)):
                    if len(out) >= limit:
                        return
                    out.append({"op": "added", "path": format_path((*path, i)), "after": _display(b[i])})
                return
            if len(a) == len(b):
                for i, (x, y) in enumerate(zip(a, b, strict=True)):
                    walk(x, y, (*path, i))
                return
        if not json_equal(a, b):
            out.append(
                {
                    "op": "changed",
                    "path": format_path(path) or "$",
                    "before": _display(a),
                    "after": _display(b),
                }
            )

    walk(before, after, ())
    return out
