"""Client-side redaction of secrets and personal data.

The rules and their semantics mirror ``gokit/redact`` exactly (Go uses RE2;
patterns here use ASCII classes and Go's ``\\s`` set ``[\\t\\n\\f\\r ]`` so
both engines match the same text). Parity is verified against
``packages/contracts/fixtures/redaction.json`` and by differential tests that
run random inputs through the Go reference implementation.

Strategies:

``mask``  replace a match with ``[REDACTED:<kind>]``
``hash``  replace a match with ``[HASH:<kind>:<12 hex of sha256>]`` so equal
          values stay comparable without being revealed
``drop``  drop the whole value when anything sensitive is found
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "PII_RULES",
    "SECRET_RULES",
    "RedactionConfig",
    "Redactor",
    "Rule",
    "Strategy",
    "truncate",
]

Strategy = Literal["mask", "hash", "drop"]

_WS = r"[\t\n\f\r ]"  # Go RE2 \s


@dataclass(frozen=True)
class Rule:
    """One detector. ``group`` selects the sub-match that is replaced."""

    kind: str
    pattern: re.Pattern[str]
    group: int = 0
    valid: Callable[[str], bool] | None = None


def _luhn(s: str) -> bool:
    digits = [ord(c) - 48 for c in s if "0" <= c <= "9"]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    double = False
    for d in reversed(digits):
        if double:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        double = not double
    return total % 10 == 0


def _phone_digits(s: str) -> bool:
    n = sum(1 for c in s if "0" <= c <= "9")
    return 9 <= n <= 15


def _re(pattern: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(pattern, flags | re.ASCII)


SECRET_RULES: tuple[Rule, ...] = (
    Rule("private_key", _re(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    Rule("jwt", _re(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")),
    Rule("bearer", _re(rf"\bbearer{_WS}+([A-Za-z0-9._~+/=-]{{8,}})", re.IGNORECASE), group=1),
    Rule(
        "api_key",
        _re(
            r"\b(?:atk_[a-z0-9]{8}_[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}"
            r"|gh[pousr]_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{10,})"
        ),
    ),
    Rule(
        "credential",
        _re(
            r"\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret)\b"
            rf"[\"']?{_WS}*[:=]{_WS}*[\"']?([^\t\n\f\r \"',;]{{4,}})",
            re.IGNORECASE,
        ),
        group=1,
    ),
)

PII_RULES: tuple[Rule, ...] = (
    Rule("email", _re(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    Rule("card", _re(r"\b(?:\d[ -]?){12,18}\d\b"), valid=_luhn),
    Rule(
        "phone",
        _re(
            rf"\+\d{{1,3}}(?:[\t\n\f\r .-]?\d{{1,4}}){{2,5}}\b"
            rf"|\(\d{{3}}\){_WS}?\d{{3}}[\t\n\f\r .-]\d{{4}}\b"
            r"|\b\d{3}[.-]\d{3}[.-]\d{4}\b"
        ),
        valid=_phone_digits,
    ),
)

_MARKER = _re(r"\[(?:REDACTED:[a-z_]+|HASH:[a-z_]+:[0-9a-f]{12})\]")
_MAX_ROUNDS = 8


@dataclass(frozen=True)
class RedactionConfig:
    """What the SDK redacts before any content leaves the process.

    ``pii`` enables email/card/phone detection (secrets are always detected).
    ``custom_patterns`` are extra regular expressions (kind ``custom``).
    ``json_paths`` redact whole fields of structured values such as tool
    arguments, e.g. ``$.customer.email`` or ``$.items[*].card_number``.
    """

    strategy: Strategy = "mask"
    pii: bool = True
    custom_patterns: tuple[str, ...] = ()
    json_paths: tuple[str, ...] = ()

    def redactor(self) -> Redactor:
        rules = list(SECRET_RULES)
        if self.pii:
            rules.extend(PII_RULES)
        rules.extend(Rule("custom", re.compile(p)) for p in self.custom_patterns)
        return Redactor(rules, self.strategy, json_paths=self.json_paths)


@dataclass
class Redactor:
    """Applies rules with a strategy. Instances are immutable after creation."""

    rules: Sequence[Rule]
    strategy: Strategy = "mask"
    json_paths: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        self._paths = [_parse_path(p) for p in self.json_paths]

    @classmethod
    def secrets(cls, strategy: Strategy = "mask") -> Redactor:
        return cls(SECRET_RULES, strategy)

    @classmethod
    def all(cls, strategy: Strategy = "mask") -> Redactor:
        return cls((*SECRET_RULES, *PII_RULES), strategy)

    # -- text -------------------------------------------------------------

    def text(self, s: str) -> tuple[str | None, bool]:
        """Redact free text. Returns (result, changed); with the ``drop``
        strategy the result is None when anything sensitive was found."""
        out, changed = self._fixpoint(s)
        if changed and self.strategy == "drop":
            return None, True
        return out, changed

    def _fixpoint(self, s: str) -> tuple[str, bool]:
        changed = False
        for _ in range(_MAX_ROUNDS):
            out, c = self._pass(s)
            if not c:
                break
            s, changed = out, True
        return s, changed

    def _pass(self, s: str) -> tuple[str, bool]:
        changed = False
        for rule in self.rules:
            s, c = self._outside_markers(rule, s)
            changed = changed or c
        return s, changed

    def _outside_markers(self, rule: Rule, s: str) -> tuple[str, bool]:
        spans = [m.span() for m in _MARKER.finditer(s)]
        if not spans:
            return self._apply(rule, s)
        parts: list[str] = []
        changed = False
        prev = 0
        for start, end in spans:
            seg, c = self._apply(rule, s[prev:start])
            parts.append(seg)
            parts.append(s[start:end])
            changed = changed or c
            prev = end
        seg, c = self._apply(rule, s[prev:])
        parts.append(seg)
        return "".join(parts), changed or c

    def _apply(self, rule: Rule, s: str) -> tuple[str, bool]:
        parts: list[str] = []
        prev = 0
        changed = False
        for m in rule.pattern.finditer(s):
            start, end = m.span(rule.group)
            if start < 0:
                continue
            target = s[start:end]
            if not target or (rule.valid is not None and not rule.valid(target)):
                continue
            parts.append(s[prev:start])
            parts.append(self._replacement(rule.kind, target))
            prev = end
            changed = True
        if not changed:
            return s, False
        parts.append(s[prev:])
        return "".join(parts), True

    def _replacement(self, kind: str, value: str) -> str:
        if self.strategy == "hash":
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
            return f"[HASH:{kind}:{digest}]"
        return f"[REDACTED:{kind}]"

    # -- structured values ------------------------------------------------

    def value(self, v: Any) -> tuple[Any, bool]:
        """Redact a JSON-like value: configured JSON paths first (whole
        fields), then every string leaf. Keys are never redacted. With the
        ``drop`` strategy, sensitive fields are removed."""
        out = copy.deepcopy(v)
        changed = False
        for path in self._paths:
            out, c = _apply_path(out, path, self)
            changed = changed or c
        out, c = self._leaves(out)
        return out, changed or c

    def _leaves(self, v: Any) -> tuple[Any, bool]:
        if isinstance(v, str):
            red, changed = self.text(v)
            return red, changed
        if isinstance(v, Mapping):
            result: dict[str, Any] = {}
            changed = False
            for k, item in v.items():
                red, c = self._leaves(item)
                changed = changed or c
                if red is None and c and self.strategy == "drop":
                    continue
                result[k] = red
            return result, changed
        if isinstance(v, list | tuple):
            items: list[Any] = []
            changed = False
            for item in v:
                red, c = self._leaves(item)
                changed = changed or c
                if red is None and c and self.strategy == "drop":
                    continue
                items.append(red)
            return items, changed
        return v, False

    def field_replacement(self, v: Any) -> str | None:
        if self.strategy == "drop":
            return None
        if self.strategy == "hash":
            from agenttwin.hashing import canonical_json

            try:
                encoded = canonical_json(v)
            except (TypeError, ValueError):
                encoded = repr(v)
            return f"[HASH:field:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:12]}]"
        return "[REDACTED:field]"


# -- JSON paths (a deliberately small subset: $.a.b, $.a[*].b, $.a[0], $['k']) --

_Segment = str | int | Literal["*"]
_PATH_TOKEN = re.compile(r"\.([A-Za-z0-9_\-]+)|\[(\*|\d+)\]|\['([^']*)'\]")


def _parse_path(path: str) -> list[_Segment]:
    if not path.startswith("$"):
        raise ValueError(f"json path must start with '$': {path!r}")
    segs: list[_Segment] = []
    pos = 1
    while pos < len(path):
        m = _PATH_TOKEN.match(path, pos)
        if not m:
            raise ValueError(f"unsupported json path syntax at {pos}: {path!r}")
        name, index, quoted = m.groups()
        if name is not None:
            segs.append(name)
        elif quoted is not None:
            segs.append(quoted)
        elif index == "*":
            segs.append("*")
        else:
            segs.append(int(index))
        pos = m.end()
    if not segs:
        raise ValueError(f"json path selects the whole document: {path!r}")
    return segs


def _apply_path(v: Any, path: list[_Segment], red: Redactor) -> tuple[Any, bool]:
    if not path:
        return v, False
    head, rest = path[0], path[1:]
    changed = False
    if isinstance(v, dict):
        keys: Iterable[str] = list(v.keys()) if head == "*" else ([head] if isinstance(head, str) else [])
        for k in keys:
            if k not in v:
                continue
            if rest:
                v[k], c = _apply_path(v[k], rest, red)
            else:
                rep = red.field_replacement(v[k])
                if rep is None:
                    del v[k]
                else:
                    v[k] = rep
                c = True
            changed = changed or c
    elif isinstance(v, list):
        if head == "*":
            idxs = list(range(len(v)))
        elif isinstance(head, int) and head < len(v):
            idxs = [head]
        else:
            idxs = []
        drop: list[int] = []
        for i in idxs:
            if rest:
                v[i], c = _apply_path(v[i], rest, red)
            else:
                rep = red.field_replacement(v[i])
                if rep is None:
                    drop.append(i)
                else:
                    v[i] = rep
                c = True
            changed = changed or c
        for i in reversed(drop):
            del v[i]
    return v, changed


def truncate(s: str, max_bytes: int) -> tuple[str, bool]:
    """Shorten ``s`` to at most ``max_bytes`` UTF-8 bytes on a character
    boundary, appending a marker (same contract as gokit/redact.Truncate)."""
    raw = s.encode("utf-8")
    if max_bytes <= 0 or len(raw) <= max_bytes:
        return s, False
    marker = "…[truncated]"
    cut = max(max_bytes - len(marker.encode("utf-8")), 0)
    while cut > 0 and (raw[cut] & 0xC0) == 0x80:
        cut -= 1
    return raw[:cut].decode("utf-8") + marker, True
