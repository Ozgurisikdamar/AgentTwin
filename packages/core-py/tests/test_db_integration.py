"""Migrator and pool against a real PostgreSQL (ADR-0012)."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from agenttwin_core.db import Migrator, connect, jsonb, load_migrations, transaction
from agenttwin_core.testing import temp_database

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


def write_migrations(tmp: Path) -> Path:
    d = tmp / "migrations"
    d.mkdir()
    (d / "0001_init.up.sql").write_text(
        "CREATE TABLE thing (id uuid PRIMARY KEY, doc jsonb NOT NULL);\n"
        "CREATE INDEX thing_doc_idx ON thing USING gin (doc);\n"
    )
    (d / "0001_init.down.sql").write_text("DROP TABLE thing;\n")
    (d / "0002_more.up.sql").write_text("ALTER TABLE thing ADD COLUMN note text;\n")
    (d / "0002_more.down.sql").write_text("ALTER TABLE thing DROP COLUMN note;\n")
    (d / "README.md").write_text("not a migration")
    return d


async def test_migrator_lifecycle(tmp_path: Path) -> None:
    d = write_migrations(tmp_path)
    migs = load_migrations(d)
    assert [m.version for m in migs] == ["0001", "0002"]
    assert migs[0].checksum == hashlib.sha256((d / "0001_init.up.sql").read_bytes()).hexdigest()
    async with temp_database() as url:
        pool = await connect(url, schema="svc_test", max_size=4)
        try:
            m = Migrator(pool, "svc_test", migs)
            assert await m.pending() == ["0001_init", "0002_more"]
            assert await m.up() == 2
            assert await m.up() == 0
            assert await m.pending() == []
            # search_path: unqualified names resolve inside the service schema.
            async with transaction(pool) as conn:
                await conn.execute(
                    "INSERT INTO thing (id, doc, note) VALUES (%s, %s, %s)",
                    ("0190f3b4-0000-7000-8000-000000000001", jsonb({"a": [1, 2.5], "ok": True}), "n"),
                )
            async with pool.connection() as conn:
                row = await (await conn.execute("SELECT id, doc FROM svc_test.thing")).fetchone()
                assert row == {
                    "id": "0190f3b4-0000-7000-8000-000000000001",
                    "doc": {"a": [1, 2.5], "ok": True},
                }
                ledger = await (
                    await conn.execute(
                        "SELECT version, name, checksum FROM svc_test.schema_migrations ORDER BY 1"
                    )
                ).fetchall()
                assert [(r["version"], r["name"]) for r in ledger] == [("0001", "init"), ("0002", "more")]
                assert ledger[0]["checksum"] == migs[0].checksum
                cols = await (
                    await conn.execute(
                        "SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_schema = 'svc_test' AND table_name = 'schema_migrations' "
                        "ORDER BY ordinal_position"
                    )
                ).fetchall()
                # Same ledger as gokit/db (Go and Python services share the discipline).
                assert [(c["column_name"], c["data_type"]) for c in cols] == [
                    ("version", "text"),
                    ("name", "text"),
                    ("checksum", "text"),
                    ("applied_at", "timestamp with time zone"),
                ]
            assert await m.down(1) == 1
            assert await m.pending() == ["0002_more"]
            assert await m.up() == 1
            # History must not be rewritten silently.
            (d / "0001_init.up.sql").write_text("CREATE TABLE thing (id uuid PRIMARY KEY);\n")
            with pytest.raises(RuntimeError, match="checksum mismatch"):
                await Migrator(pool, "svc_test", load_migrations(d)).up()
        finally:
            await pool.close()


async def test_failed_migration_rolls_back_atomically(tmp_path: Path) -> None:
    d = tmp_path / "m"
    d.mkdir()
    (d / "0001_ok.up.sql").write_text("CREATE TABLE a (id int);\n")
    (d / "0002_bad.up.sql").write_text("CREATE TABLE b (id int);\nSELECT * FROM does_not_exist;\n")
    async with temp_database() as url:
        pool = await connect(url, schema="svc_bad", max_size=2)
        try:
            m = Migrator(pool, "svc_bad", load_migrations(d))
            with pytest.raises(Exception, match="does_not_exist"):
                await m.up()
            assert await m.pending() == ["0002_bad"]
            async with pool.connection() as conn:
                tables = await (
                    await conn.execute(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'svc_bad' ORDER BY 1"
                    )
                ).fetchall()
            assert [t["tablename"] for t in tables] == ["a", "schema_migrations"]
        finally:
            await pool.close()


async def test_concurrent_migrators_apply_once(tmp_path: Path) -> None:
    d = tmp_path / "m"
    d.mkdir()
    for i in range(1, 6):
        (d / f"{i:04d}_step{i}.up.sql").write_text(f"CREATE TABLE t{i} (id int);\nSELECT pg_sleep(0.05);\n")
    async with temp_database() as url:
        pools = [await connect(url, schema="svc_race", max_size=2) for _ in range(4)]
        try:
            counts = await asyncio.gather(*(Migrator(p, "svc_race", load_migrations(d)).up() for p in pools))
            assert sum(counts) == 5
            assert await Migrator(pools[0], "svc_race", load_migrations(d)).pending() == []
        finally:
            for p in pools:
                await p.close()


async def test_jsonb_rejects_non_finite_numbers() -> None:
    async with temp_database() as url:
        pool = await connect(url, schema="svc_json", max_size=1)
        try:
            async with pool.connection() as conn:
                with pytest.raises(ValueError, match="JSON compliant"):
                    await conn.execute("SELECT %s::jsonb", (jsonb({"x": float("nan")}),))
        finally:
            await pool.close()
