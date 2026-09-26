"""Retry-After as providers send it: seconds or an HTTP date, bounded either way."""

from datetime import UTC, datetime

import pytest

from agenttwin_core.retry import retry_after_seconds

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("header", "want"),
    [
        ("3", 3.0),
        ("0.5", 0.5),
        ("120", 10.0),  # capped
        ("-5", 0.0),
        ("Sat, 26 Sep 2026 12:00:04 GMT", 4.0),  # an HTTP date: seconds from now
        ("Sat, 26 Sep 2026 12:05:00 GMT", 10.0),
        ("Sat, 26 Sep 2026 11:59:00 GMT", 0.0),  # already past
        ("Sat, 26 Sep 2026 12:00:06 -0000", 6.0),  # a zone-less date is UTC
        ("   ", 2.0),
        ("", 2.0),  # absent: the caller's backoff
        (None, 2.0),
        ("soon", 2.0),
        ("nan", 2.0),
        ("inf", 2.0),
    ],
)
def test_retry_after_seconds(header: str | None, want: float) -> None:
    assert retry_after_seconds(header, fallback=2.0, cap=10.0, now=NOW) == want
