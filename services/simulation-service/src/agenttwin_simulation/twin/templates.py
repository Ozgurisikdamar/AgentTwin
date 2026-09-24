"""Templates in twin definitions and fault bodies.

A string may contain placeholders in braces::

    "{order_id}"                 the argument ``order_id``
    "{args.customer.id}"         a nested argument
    "{state.orders.ORD-1.total}" a value of the twin's state
    "{value}" / "{value.status}" the entity a handler reads or mutates
    "{result}"                   (fault bodies) what the tool would have returned
    "{tenant}" "{idempotency_key}" "{now}" "{id}" "{call.number}" "{call.seq}"

A string that is exactly one placeholder keeps the value's type (a number
stays a number); placeholders mixed with text are interpolated as text. A
trailing ``?`` (``{reason?}``) makes a placeholder optional: a missing value
renders as null (or as nothing inside text).
``{{`` and ``}}`` are literal braces. Any other identifier is shorthand for
an argument (``{order_id}`` is ``{args.order_id}``).

In *path* templates (``orders.{order_id}``) a placeholder always becomes one
whole path segment, whatever its value contains, so an argument such as
``"ORD-1.tenant"`` cannot traverse into another part of the state.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agenttwin_core.paths import MISSING, PathError, Segment, get_path, parse_path

__all__ = ["Env", "TemplateError", "check_template", "render", "render_path"]

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MAX_KEY = 500
_SENTINEL = "\x00"
_INDEX_SENTINEL = "\x01"


class TemplateError(ValueError):
    """A template could not be rendered. When the cause is an argument of the
    call (missing, or unusable in a path) ``argument`` names it, so the caller
    can answer with an invalid-request error instead of a twin error."""

    def __init__(self, message: str, *, argument: str | None = None, missing: bool = False) -> None:
        super().__init__(message)
        self.argument = argument
        self.missing = missing


@dataclass
class Env:
    args: Mapping[str, Any] = field(default_factory=dict)
    state: Mapping[str, Any] = field(default_factory=dict)
    value: Any = MISSING
    result: Any = MISSING
    tenant: str | None = None
    idempotency_key: str | None = None
    now: str = ""
    seq: int = 0
    call_number: int = 0

    def lookup(self, expr: str) -> Any:
        return self.resolve(expr)[0]

    def resolve(self, expr: str) -> tuple[Any, str | None]:
        """The value of a placeholder and the argument it refers to (if any).
        A trailing ``?`` makes the placeholder optional: missing becomes null."""
        optional = expr.endswith("?")
        if optional:
            expr = expr[:-1]
        m = _IDENT.match(expr)
        if not m:
            raise TemplateError(f"invalid placeholder {{{expr}}}")
        root, rest = m.group(0), expr[m.end() :]
        if rest and rest[0] not in ".[":
            raise TemplateError(f"invalid placeholder {{{expr}}}")
        try:
            segments = parse_path(rest) if rest else ()
        except PathError as err:
            raise TemplateError(f"invalid placeholder {{{expr}}}: {err}") from None
        argument: str | None = None
        base: Any
        if root == "args":
            base = self.args
            first = segments[0] if segments else None
            argument = first if isinstance(first, str) else None
        elif root == "state":
            base = self.state
        elif root == "value":
            base = self.value
        elif root == "result":
            base = self.result
        elif root == "tenant":
            base = self.tenant if self.tenant is not None else MISSING
        elif root == "idempotency_key":
            base = self.idempotency_key if self.idempotency_key is not None else MISSING
        elif root == "now":
            base = self.now or MISSING
        elif root == "id":
            base = f"{self.seq:04d}"
        elif root == "call":
            base = {"number": self.call_number, "seq": self.seq}
        else:
            base = self.args
            segments = (root, *segments)
            argument = root
        value = get_path(base, segments) if segments else base
        if value is MISSING:
            if optional:
                return None, argument
            if argument is not None:
                raise TemplateError(f"{argument} is required", argument=argument, missing=True)
            raise TemplateError(f"{{{expr}}} has no value")
        return value, argument


def _tokens(template: str) -> list[tuple[bool, str]]:
    """``(is_placeholder, text)`` tokens; ``{{``/``}}`` unescaped."""
    out: list[tuple[bool, str]] = []
    buf: list[str] = []
    i, n = 0, len(template)
    while i < n:
        c = template[i]
        if c == "{" and template.startswith("{{", i):
            buf.append("{")
            i += 2
        elif c == "}" and template.startswith("}}", i):
            buf.append("}")
            i += 2
        elif c == "{":
            close = template.find("}", i + 1)
            if close < 0:
                raise TemplateError(f"unterminated placeholder in {template[:80]!r}")
            if buf:
                out.append((False, "".join(buf)))
                buf = []
            expr = template[i + 1 : close].strip()
            if not expr:
                raise TemplateError(f"empty placeholder in {template[:80]!r}")
            out.append((True, expr))
            i = close + 1
        elif c == "}":
            raise TemplateError(f"unbalanced '}}' in {template[:80]!r}")
        else:
            buf.append(c)
            i += 1
    if buf:
        out.append((False, "".join(buf)))
    return out


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def render(template: Any, env: Env) -> Any:
    """Renders strings, lists and objects recursively (object keys are literal)."""
    if isinstance(template, str):
        if "{" not in template and "}" not in template:
            return template
        tokens = _tokens(template)
        if len(tokens) == 1 and tokens[0][0]:
            return copy.deepcopy(env.lookup(tokens[0][1]))
        return "".join(_text(env.lookup(t)) if is_ph else t for is_ph, t in tokens)
    if isinstance(template, Mapping):
        return {str(k): render(v, env) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, env) for v in template]
    return copy.deepcopy(template)


def _key(env: Env, expr: str) -> str:
    value, argument = env.resolve(expr)
    name = argument or f"{{{expr}}}"
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise TemplateError(f"{name} must be a string or a number", argument=argument)
    text = _text(value)
    if not text or len(text) > _MAX_KEY:
        raise TemplateError(f"{name} must be 1-{_MAX_KEY} characters", argument=argument)
    return text


def _parse_template_path(template: str) -> tuple[tuple[Segment, ...], list[str]]:
    if _SENTINEL in template or _INDEX_SENTINEL in template:
        raise TemplateError("path templates cannot contain control characters")
    exprs: list[str] = []
    parts: list[str] = []
    tokens = _tokens(template)
    for idx, (is_ph, text) in enumerate(tokens):
        if not is_ph:
            parts.append(text)
            continue
        n = len(exprs)
        exprs.append(text)
        prev = tokens[idx - 1][1] if idx > 0 and not tokens[idx - 1][0] else ""
        nxt = tokens[idx + 1][1] if idx + 1 < len(tokens) and not tokens[idx + 1][0] else ""
        if prev.endswith("[") and nxt.startswith("]"):
            parts.append(f"'{_INDEX_SENTINEL}{n}{_INDEX_SENTINEL}'")
        else:
            parts.append(f"{_SENTINEL}{n}{_SENTINEL}")
    try:
        return parse_path("".join(parts)), exprs
    except PathError as err:
        raise TemplateError(f"invalid path template {template!r}: {err}") from None


_SENT_RE = re.compile(f"{_SENTINEL}(\\d+){_SENTINEL}")


def render_path(template: str, env: Env) -> tuple[Segment, ...]:
    """A path template rendered to segments; placeholders never change the
    path's structure."""
    segments, exprs = _parse_template_path(template)
    out: list[Segment] = []
    for seg in segments:
        if not isinstance(seg, str) or (_SENTINEL not in seg and _INDEX_SENTINEL not in seg):
            out.append(seg)
            continue
        if (
            seg.startswith(_INDEX_SENTINEL)
            and seg.endswith(_INDEX_SENTINEL)
            and seg.count(_INDEX_SENTINEL) == 2
        ):
            expr = exprs[int(seg[1:-1])]
            value = env.lookup(expr)
            out.append(value if isinstance(value, int) and not isinstance(value, bool) else _key(env, expr))
            continue
        whole = _SENT_RE.fullmatch(seg)
        if whole:
            out.append(_key(env, exprs[int(whole.group(1))]))
            continue
        text = _SENT_RE.sub(lambda m: _key(env, exprs[int(m.group(1))]), seg)
        if len(text) > _MAX_KEY:
            raise TemplateError(f"path segment longer than {_MAX_KEY} characters")
        out.append(text)
    return tuple(out)


def check_template(template: Any, *, path: bool = False) -> str | None:
    """A syntax problem of a template (checked when a twin is registered)."""
    try:
        if path:
            if not isinstance(template, str):
                return "must be a string"
            _parse_template_path(template)
            return None
        if isinstance(template, str):
            for is_ph, text in _tokens(template):
                if is_ph:
                    text = text.removesuffix("?")
                    m = _IDENT.match(text)
                    if not m or (text[m.end() :] and text[m.end()] not in ".["):
                        return f"invalid placeholder {{{text}}}"
                    rest = text[m.end() :]
                    if rest:
                        parse_path(rest)
        elif isinstance(template, Mapping):
            for v in template.values():
                if problem := check_template(v):
                    return problem
        elif isinstance(template, list):
            for v in template:
                if problem := check_template(v):
                    return problem
    except (TemplateError, PathError) as err:
        return str(err)
    return None
