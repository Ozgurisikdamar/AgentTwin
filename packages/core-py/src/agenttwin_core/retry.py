"""How long to wait before retrying a provider that answered 429 or 503."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

__all__ = ["retry_after_seconds"]


def retry_after_seconds(value: str | None, fallback: float, cap: float, now: datetime | None = None) -> float:
    """The wait a ``Retry-After`` header asks for, within ``[0, cap]``.

    RFC 9110 allows seconds or an HTTP date; providers send both. An absent
    or unreadable header, or one that is not a finite number, falls back to
    ``fallback`` (the caller's own backoff), so a bad header neither stalls
    a caller nor makes it hammer a busy provider."""
    raw = value or ""
    wait = fallback
    if raw:
        try:
            wait = float(raw)
        except ValueError:
            try:
                when = parsedate_to_datetime(raw)
            except (TypeError, ValueError, IndexError):
                wait = fallback
            else:
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                wait = (when - (now or datetime.now(UTC))).total_seconds()
        if not math.isfinite(wait):
            wait = fallback
    return max(0.0, min(wait, cap))
