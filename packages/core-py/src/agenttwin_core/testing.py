"""Real-infrastructure helpers for integration tests (ADR-0012), shared by
the Python services' test suites: an isolated PostgreSQL database per test
and the RabbitMQ URL. Without the environment the tests skip — or fail when
``AGENTTWIN_REQUIRE_INTEGRATION=1`` so CI can never silently skip them.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

__all__ = ["amqp_url", "require_or_skip", "temp_database"]


def require_or_skip(what: str) -> None:
    import pytest

    if os.environ.get("AGENTTWIN_REQUIRE_INTEGRATION") == "1":
        pytest.fail(f"{what} is required for integration tests but not configured")
    pytest.skip(f"{what} not configured; skipping integration test")


def amqp_url() -> str:
    url = os.environ.get("AGENTTWIN_TEST_AMQP_URL", "")
    if not url:
        require_or_skip("AGENTTWIN_TEST_AMQP_URL")
    return url


@asynccontextmanager
async def temp_database() -> AsyncIterator[str]:
    """Creates a fresh database on AGENTTWIN_TEST_DATABASE_URL's server and
    yields its URL; it is dropped (with FORCE) afterwards."""
    base = os.environ.get("AGENTTWIN_TEST_DATABASE_URL", "")
    if not base:
        require_or_skip("AGENTTWIN_TEST_DATABASE_URL")
    name = "at_test_" + secrets.token_hex(6)
    async with await psycopg.AsyncConnection.connect(base, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    parts = urlsplit(base)
    url = urlunsplit(parts._replace(path="/" + name))
    try:
        yield url
    finally:
        async with await psycopg.AsyncConnection.connect(base, autocommit=True) as admin:
            await admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )
