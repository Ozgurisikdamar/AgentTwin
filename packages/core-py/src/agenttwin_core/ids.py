"""Identifiers. UUIDv7 (RFC 9562) keeps primary keys time-ordered like the Go
services' ``ids.New`` so B-tree inserts stay local and cursors sort by time.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

__all__ = ["new_id", "valid_uuid"]

_lock = threading.Lock()
_last_ms = 0
_seq = 0


def new_id() -> str:
    """A new UUIDv7 string, monotonic within this process.

    Within one millisecond the 12-bit ``rand_a`` field is used as a counter
    (RFC 9562 method 1) seeded randomly each millisecond; on overflow the
    timestamp is advanced by one millisecond.
    """
    global _last_ms, _seq
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms > _last_ms:
            _last_ms = ms
            _seq = int.from_bytes(os.urandom(2), "big") & 0x3FF  # leave headroom for increments
        else:
            _seq += 1
            if _seq > 0xFFF:
                _last_ms += 1
                _seq = 0
            ms = _last_ms
        seq = _seq
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76  # version
    value |= seq << 64
    value |= 0b10 << 62  # variant
    value |= rand_b
    return str(uuid.UUID(int=value))


def valid_uuid(value: object) -> bool:
    """True for a canonical 36-character UUID string."""
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True
