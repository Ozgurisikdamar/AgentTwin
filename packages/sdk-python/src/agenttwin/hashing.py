"""Canonical JSON and SHA-256 content identities.

The canonical form is shared with the Go services (``gokit/hashx``) and the
TypeScript SDK, and all of them are verified against
``packages/contracts/fixtures/canonical-json.json``:

* object keys sorted by code point (identical to UTF-8 byte order);
* no insignificant whitespace, no HTML escaping;
* integers without fraction or exponent; integral floats below 1e21 as their
  shortest round-trip digits padded with zeros (Go's ``'f', -1`` format);
* other numbers in the shortest round-trip form, printed like Go's
  ``strconv.FormatFloat(f, 'g', -1, 64)`` (exponent form below 1e-4 and at or
  above 1e6).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

__all__ = ["canonical_json", "content_hash", "sha256_hex"]

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_SURROGATE = re.compile("[\ud800-\udfff]")


def sha256_hex(data: bytes | str) -> str:
    """Hex SHA-256 of bytes (strings are UTF-8 encoded)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def content_hash(value: Any) -> str:
    """SHA-256 of the canonical JSON form of ``value``."""
    return sha256_hex(canonical_json(value))


def canonical_json(value: Any) -> str:
    """Return the canonical JSON encoding of ``value``.

    Raises TypeError for values JSON cannot represent and ValueError for
    non-finite numbers.
    """
    out: list[str] = []
    _write(value, out)
    return "".join(out)


def _write(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        out.append(str(value) if _INT64_MIN <= value <= _INT64_MAX else _float(float(value)))
    elif isinstance(value, float):
        out.append(_float(value))
    elif isinstance(value, str):
        out.append(_string(value))
    elif isinstance(value, Mapping):
        items = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"canonical json: object keys must be strings, got {type(key).__name__}")
            items.append((key, item))
        items.sort(key=lambda kv: kv[0])
        out.append("{")
        for i, (key, item) in enumerate(items):
            if i:
                out.append(",")
            out.append(_string(key))
            out.append(":")
            _write(item, out)
        out.append("}")
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _write(item, out)
        out.append("]")
    else:
        raise TypeError(f"canonical json: unsupported type {type(value).__name__}")


def _string(s: str) -> str:
    # json.dumps escapes exactly what Go's encoder escapes for control
    # characters, quotes and backslashes; Go additionally escapes U+2028 and
    # U+2029 and replaces invalid code points with U+FFFD.
    encoded = json.dumps(s, ensure_ascii=False)
    encoded = encoded.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return _SURROGATE.sub("\\\\ufffd", encoded)


def _float(f: float) -> str:
    if math.isnan(f) or math.isinf(f):
        raise ValueError(f"canonical json: non-finite number {f!r}")
    if f == math.trunc(f) and abs(f) < 1e21:
        return _go_fixed(f)
    return _go_shortest_g(f)


def _go_fixed(f: float) -> str:
    """Format an integral float like Go's strconv.FormatFloat(f, 'f', -1, 64):
    shortest round-trip digits padded with zeros (so 5.9449638243910850e20 is
    594496382439108500000, not its exact binary value). Zero of either sign is
    "0" so the encoding stays a fixed point."""
    if f == 0:
        return "0"
    text = format(Decimal(repr(f)), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _go_shortest_g(f: float) -> str:
    """Format like Go's strconv.FormatFloat(f, 'g', -1, 64)."""
    sign, digit_tuple, exponent = Decimal(repr(f)).as_tuple()
    if not isinstance(exponent, int):  # only for NaN/Infinity, rejected earlier
        raise ValueError(f"canonical json: non-finite number {f!r}")
    digits = "".join(str(d) for d in digit_tuple).lstrip("0")
    # Strip trailing zeros while keeping the value (value = digits * 10**exponent).
    stripped = digits.rstrip("0")
    exponent += len(digits) - len(stripped)
    digits = stripped or "0"
    nd = len(digits)
    dp = nd + exponent  # value = 0.<digits> * 10**dp
    exp10 = dp - 1
    neg = "-" if sign else ""
    if exp10 < -4 or exp10 >= 6:
        mantissa = digits[0] + ("." + digits[1:] if nd > 1 else "")
        esign = "-" if exp10 < 0 else "+"
        return f"{neg}{mantissa}e{esign}{abs(exp10):02d}"
    if dp <= 0:
        return f"{neg}0.{'0' * -dp}{digits}"
    if dp >= nd:
        return f"{neg}{digits}{'0' * (dp - nd)}"
    return f"{neg}{digits[:dp]}.{digits[dp:]}"
