"""PostgreSQL access of the evaluation service (``evaluation`` schema)."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agenttwin_core.db import Conn, Pool, jsonb

__all__ = ["SCHEMA", "Cursor", "Row", "Scope", "Store", "decode_cursor", "encode_cursor"]

Row = dict[str, Any]
SCHEMA = "evaluation"


@dataclass(frozen=True)
class Cursor:
    key: str
    id: str


def encode_cursor(key: datetime | str, row_id: str) -> str:
    value = key.isoformat() if isinstance(key, datetime) else key
    return base64.urlsafe_b64encode(json.dumps({"k": value, "i": row_id}).encode()).decode().rstrip("=")


def decode_cursor(token: str) -> Cursor | None:
    if not token:
        return None
    if len(token) > 512:
        raise ValueError("cursor is malformed")
    try:
        raw = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        return Cursor(key=str(raw["k"]), id=str(raw["i"]))
    except (ValueError, KeyError, TypeError):
        raise ValueError("cursor is malformed") from None


@dataclass(frozen=True)
class Scope:
    """The projects a caller may read: every project of the organization
    (``project_ids=None``) or an explicit list."""

    org_id: str
    project_ids: tuple[str, ...] | None = None

    def clause(self, alias: str = "") -> tuple[str, list[Any]]:
        col = f"{alias}." if alias else ""
        if self.project_ids is None:
            return f"{col}organization_id = %s", [self.org_id]
        return f"{col}organization_id = %s AND {col}project_id = ANY(%s)", [
            self.org_id,
            list(self.project_ids),
        ]


class Store:
    def __init__(self, pool: Pool) -> None:
        self.pool = pool

    # ------------------------------------------------------------ helpers

    @staticmethod
    async def _one(conn: Conn, query: str, params: Sequence[Any] = ()) -> Row | None:
        cur = await conn.execute(query, params)
        return await cur.fetchone()

    @staticmethod
    async def _all(conn: Conn, query: str, params: Sequence[Any] = ()) -> list[Row]:
        cur = await conn.execute(query, params)
        return await cur.fetchall()

    async def one(self, query: str, params: Sequence[Any] = ()) -> Row | None:
        async with self.pool.connection() as conn:
            return await self._one(conn, query, params)

    async def all(self, query: str, params: Sequence[Any] = ()) -> list[Row]:
        async with self.pool.connection() as conn:
            return await self._all(conn, query, params)

    # ------------------------------------------------------------ datasets

    async def insert_dataset(
        self, conn: Conn, dataset: Mapping[str, Any], version: Mapping[str, Any]
    ) -> Row | None:
        """None when the project already has a dataset of that name."""
        row = await self._one(
            conn,
            """INSERT INTO dataset (id, organization_id, project_id, name, description, owner, tags,
                   latest_version, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s)
               ON CONFLICT (project_id, name) DO NOTHING RETURNING *""",
            (
                dataset["id"],
                dataset["organization_id"],
                dataset["project_id"],
                dataset["name"],
                dataset.get("description"),
                dataset.get("owner"),
                list(dataset.get("tags") or []),
                dataset["created_by"],
            ),
        )
        if row is not None:
            await self._insert_version(conn, str(dataset["id"]), 1, version)
        return row

    async def _insert_version(self, conn: Conn, dataset_id: str, version: int, v: Mapping[str, Any]) -> None:
        cases = list(v["cases"])
        await conn.execute(
            """INSERT INTO dataset_version (id, dataset_id, version, cases, case_count, note, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (v["id"], dataset_id, version, jsonb(cases), len(cases), v.get("note"), v["created_by"]),
        )

    async def lock_dataset(self, conn: Conn, dataset_id: str) -> Row | None:
        """The dataset row, locked until the transaction ends: changes to one
        dataset are applied one after the other, each to the latest version."""
        return await self._one(conn, "SELECT * FROM dataset WHERE id = %s FOR UPDATE", (dataset_id,))

    async def version_in(self, conn: Conn, dataset_id: str, version: int) -> Row | None:
        return await self._one(
            conn,
            "SELECT * FROM dataset_version WHERE dataset_id = %s AND version = %s",
            (dataset_id, version),
        )

    async def add_dataset_version(self, conn: Conn, dataset_id: str, version: Mapping[str, Any]) -> Row:
        """Appends the next version (the dataset must be locked)."""
        row = await self._one(
            conn,
            """UPDATE dataset SET latest_version = latest_version + 1, updated_at = now()
               WHERE id = %s RETURNING *""",
            (dataset_id,),
        )
        assert row is not None  # noqa: S101 - locked by the caller
        await self._insert_version(conn, dataset_id, int(row["latest_version"]), version)
        return row

    async def get_dataset(self, dataset_id: str) -> Row | None:
        return await self.one("SELECT * FROM dataset WHERE id = %s", (dataset_id,))

    async def dataset_version(self, dataset_id: str, version: int) -> Row | None:
        return await self.one(
            "SELECT * FROM dataset_version WHERE dataset_id = %s AND version = %s", (dataset_id, version)
        )

    async def dataset_versions(self, dataset_id: str) -> list[Row]:
        return await self.all(
            """SELECT version, case_count, note, created_by, created_at FROM dataset_version
               WHERE dataset_id = %s ORDER BY version DESC""",
            (dataset_id,),
        )

    async def list_datasets(
        self,
        scope: Scope,
        *,
        include_archived: bool = False,
        query: str | None = None,
        after: tuple[str, str] | None = None,
        limit: int = 50,
    ) -> list[Row]:
        clause, params = scope.clause("d")
        where = [clause]
        if not include_archived:
            where.append("NOT d.archived")
        if query:
            where.append("(d.name ILIKE %s OR d.description ILIKE %s)")
            like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params += [like, like]
        if after is not None:
            where.append("(d.name, d.id) > (%s, %s::uuid)")
            params += [after[0], after[1]]
        params.append(limit)
        return await self.all(
            f"""SELECT d.*, v.case_count FROM dataset d
                JOIN dataset_version v ON v.dataset_id = d.id AND v.version = d.latest_version
                WHERE {" AND ".join(where)}
                ORDER BY d.name, d.id LIMIT %s""",  # noqa: S608 - fixed clauses, bound values
            params,
        )

    async def archive_dataset(self, conn: Conn, dataset_id: str) -> Row | None:
        return await self._one(
            conn,
            "UPDATE dataset SET archived = true, updated_at = now() WHERE id = %s RETURNING *",
            (dataset_id,),
        )

    async def last_results(self, dataset_id: str) -> dict[str, Row]:
        """For each scenario, its latest result in a completed evaluation run of
        the dataset (any version): the dataset's baseline performance."""
        rows = await self.all(
            """SELECT DISTINCT ON (c.scenario_name)
                      c.scenario_name, c.classification, r.id AS eval_run_id, r.baseline_version,
                      r.candidate_version, r.finished_at,
                      c.comparison->'baseline'->>'status' AS baseline_status,
                      c.comparison->'candidate'->>'status' AS candidate_status
               FROM eval_case_result c JOIN eval_run r ON r.id = c.eval_run_id
               WHERE r.dataset_id = %s AND r.status = 'COMPLETED'
               ORDER BY c.scenario_name, r.finished_at DESC, r.id DESC""",
            (dataset_id,),
        )
        return {str(r["scenario_name"]): r for r in rows}
