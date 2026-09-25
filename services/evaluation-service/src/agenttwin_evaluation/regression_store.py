"""PostgreSQL access of the regression miner (``evaluation`` schema, ADR-0032).

A group's label, title, component and evidence are its representative
occurrence's (unless a person triaged it); its counts, first and last seen,
versions, environments and suggested severity are derived from its
occurrences by :meth:`RegressionStore.refresh_group`, the one place that
computes them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agenttwin_core.db import Conn, Pool, jsonb

__all__ = ["MINER_ACTOR", "RegressionStore"]

Row = dict[str, Any]
MINER_ACTOR = "system:regression-miner"


class RegressionStore:
    def __init__(self, pool: Pool) -> None:
        self.pool = pool

    @staticmethod
    async def _one(conn: Conn, query: str, params: Sequence[Any] = ()) -> Row | None:
        cur = await conn.execute(query, params)
        return await cur.fetchone()

    @staticmethod
    async def _all(conn: Conn, query: str, params: Sequence[Any] = ()) -> list[Row]:
        cur = await conn.execute(query, params)
        return await cur.fetchall()

    # ------------------------------------------------------------ mining

    async def lock_agent(self, conn: Conn, project_id: str, agent: str) -> None:
        """Serializes mining for one project's agent: grouping reads the
        groups and writes a new one, and two failures of a new kind must not
        make two groups."""
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"agenttwin:regression:{project_id}:{agent}",)
        )

    async def add_flag(
        self,
        conn: Conn,
        *,
        event_id: str,
        project_id: str,
        trace_id: str,
        kind: str,
        reason: str,
        flagged_by: str | None,
    ) -> None:
        await conn.execute(
            """INSERT INTO regression_flag (event_id, project_id, trace_id, kind, reason, flagged_by)
               VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (event_id) DO NOTHING""",
            (event_id, project_id, trace_id, kind, reason, flagged_by),
        )

    async def flags_of(self, conn: Conn, project_id: str, trace_id: str) -> list[tuple[str, str]]:
        rows = await self._all(
            conn,
            """SELECT kind, reason FROM regression_flag WHERE project_id = %s AND trace_id = %s
               ORDER BY created_at, event_id""",
            (project_id, trace_id),
        )
        return [(str(r["kind"]), str(r["reason"])) for r in rows]

    async def occurrence(self, conn: Conn, project_id: str, trace_id: str) -> Row | None:
        return await self._one(
            conn,
            "SELECT * FROM regression_occurrence WHERE project_id = %s AND trace_id = %s FOR UPDATE",
            (project_id, trace_id),
        )

    async def group(self, conn: Conn, group_id: str, *, lock: bool = False) -> Row | None:
        if lock:
            return await self._one(
                conn, "SELECT * FROM regression_group WHERE id = %s FOR UPDATE", (group_id,)
            )
        return await self._one(conn, "SELECT * FROM regression_group WHERE id = %s", (group_id,))

    async def live_group(self, conn: Conn, group_id: str) -> Row | None:
        """The group itself, or the one it was merged into (followed to the
        end), locked."""
        seen: set[str] = set()
        row = await self.group(conn, group_id, lock=True)
        while row is not None and row["merged_into"] is not None and str(row["id"]) not in seen:
            seen.add(str(row["id"]))
            row = await self.group(conn, str(row["merged_into"]), lock=True)
        return row

    async def group_of_fingerprint(self, conn: Conn, project_id: str, agent: str, fp: str) -> Row | None:
        row = await self._one(
            conn,
            """SELECT group_id FROM regression_fingerprint
               WHERE project_id = %s AND agent_name = %s AND fingerprint = %s""",
            (project_id, agent, fp),
        )
        return await self.live_group(conn, str(row["group_id"])) if row else None

    async def nearest(
        self,
        conn: Conn,
        *,
        organization_id: str,
        project_id: str,
        agent: str,
        model: str,
        dims: int,
        vector: str,
        exclude_trace: str,
    ) -> tuple[str, float] | None:
        """The group of the closest occurrence of the same project, agent and
        embedding model (exact search), and its cosine similarity."""
        row = await self._one(
            conn,
            """SELECT group_id, 1 - (embedding <=> %s::vector) AS similarity
               FROM regression_occurrence
               WHERE organization_id = %s AND project_id = %s AND agent_name = %s
                 AND model = %s AND dims = %s AND embedding IS NOT NULL AND trace_id <> %s
               ORDER BY embedding <=> %s::vector, trace_id
               LIMIT 1""",
            (vector, organization_id, project_id, agent, model, dims, exclude_trace, vector),
        )
        if row is None or row["similarity"] is None:
            return None
        return str(row["group_id"]), float(row["similarity"])

    async def create_group(self, conn: Conn, group: Mapping[str, Any]) -> Row:
        row = await self._one(
            conn,
            """INSERT INTO regression_group
                 (id, organization_id, project_id, agent_name, fingerprint, title, status,
                  suggested_taxonomy, suggested_severity, taxonomy, secondary, severity, severity_reason,
                  evidence, component, representative_trace_id, first_seen, last_seen)
               VALUES (%s, %s, %s, %s, %s, %s, 'CANDIDATE', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING *""",
            (
                group["id"],
                group["organization_id"],
                group["project_id"],
                group["agent_name"],
                group["fingerprint"],
                group["title"],
                group["taxonomy"],
                group["severity"],
                group["taxonomy"],
                list(group["secondary"]),
                group["severity"],
                group["severity_reason"],
                list(group["evidence"]),
                group["component"],
                group["representative_trace_id"],
                group["seen_at"],
                group["seen_at"],
            ),
        )
        assert row is not None  # noqa: S101 - INSERT … RETURNING
        await self.map_fingerprint(
            conn, str(group["project_id"]), str(group["agent_name"]), group["fingerprint"], row["id"]
        )
        return row

    async def map_fingerprint(self, conn: Conn, project_id: str, agent: str, fp: str, group_id: Any) -> None:
        """Maps a fingerprint to a group unless it already maps to one."""
        await conn.execute(
            """INSERT INTO regression_fingerprint (project_id, agent_name, fingerprint, group_id)
               VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            (project_id, agent, fp, group_id),
        )

    async def save_occurrence(self, conn: Conn, occ: Mapping[str, Any]) -> None:
        """Inserts the occurrence, or replaces what is known about it (a
        trace's outcome or flags arrive after it was mined)."""
        await conn.execute(
            """INSERT INTO regression_occurrence
                 (organization_id, project_id, trace_id, group_id, agent_name, agent_version, environment,
                  started_at, fingerprint, title, component, taxonomy, secondary, severity, severity_reason,
                  evidence, reasons, observation, features, join_kind, join_reason, similarity, model, dims,
                  embedding)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s::vector)
               ON CONFLICT (project_id, trace_id) DO UPDATE SET
                 group_id = EXCLUDED.group_id, agent_version = EXCLUDED.agent_version,
                 environment = EXCLUDED.environment, started_at = EXCLUDED.started_at,
                 fingerprint = EXCLUDED.fingerprint, title = EXCLUDED.title, component = EXCLUDED.component,
                 taxonomy = EXCLUDED.taxonomy, secondary = EXCLUDED.secondary, severity = EXCLUDED.severity,
                 severity_reason = EXCLUDED.severity_reason, evidence = EXCLUDED.evidence,
                 reasons = EXCLUDED.reasons, observation = EXCLUDED.observation, features = EXCLUDED.features,
                 join_kind = EXCLUDED.join_kind, join_reason = EXCLUDED.join_reason,
                 similarity = EXCLUDED.similarity, model = EXCLUDED.model, dims = EXCLUDED.dims,
                 embedding = EXCLUDED.embedding, updated_at = now()""",
            (
                occ["organization_id"],
                occ["project_id"],
                occ["trace_id"],
                occ["group_id"],
                occ["agent_name"],
                occ["agent_version"],
                occ["environment"],
                occ["started_at"],
                occ["fingerprint"],
                occ["title"],
                occ["component"],
                occ["taxonomy"],
                list(occ["secondary"]),
                occ["severity"],
                occ["severity_reason"],
                list(occ["evidence"]),
                list(occ["reasons"]),
                jsonb(occ["observation"]),
                jsonb(occ["features"]),
                occ["join_kind"],
                occ["join_reason"],
                occ["similarity"],
                occ["model"],
                occ["dims"],
                occ["embedding"],
            ),
        )

    async def delete_occurrence(self, conn: Conn, project_id: str, trace_id: str) -> None:
        await conn.execute(
            "DELETE FROM regression_occurrence WHERE project_id = %s AND trace_id = %s",
            (project_id, trace_id),
        )

    async def refresh_group(self, conn: Conn, group_id: str) -> Row | None:
        """Derives the group from its occurrences: counts, first and last
        seen, versions and environments, the worst suggested severity (the
        severity too, unless a person set it) and, if the representative left,
        a new one (the worst, then the earliest). An untriaged group takes
        its representative's label, title, component and evidence."""
        await conn.execute(
            """WITH agg AS (
                    SELECT count(*) AS n, min(started_at) AS first_seen, max(started_at) AS last_seen,
                           coalesce(array_agg(DISTINCT agent_version ORDER BY agent_version)
                                    FILTER (WHERE agent_version IS NOT NULL), '{}') AS versions,
                           coalesce(array_agg(DISTINCT environment ORDER BY environment)
                                    FILTER (WHERE environment IS NOT NULL), '{}') AS environments
                    FROM regression_occurrence WHERE group_id = %s),
                worst AS (
                    SELECT severity, severity_reason FROM regression_occurrence WHERE group_id = %s
                    ORDER BY array_position(ARRAY['critical', 'high', 'medium', 'low'], severity),
                             started_at, trace_id
                    LIMIT 1),
                rep AS (
                    SELECT o.* FROM regression_occurrence o JOIN regression_group g ON g.id = o.group_id
                    WHERE o.group_id = %s
                    ORDER BY (o.trace_id = g.representative_trace_id) DESC,
                             array_position(ARRAY['critical', 'high', 'medium', 'low'], o.severity),
                             o.started_at, o.trace_id
                    LIMIT 1)
                UPDATE regression_group g SET
                    occurrence_count = agg.n,
                    first_seen = coalesce(agg.first_seen, g.first_seen),
                    last_seen = coalesce(agg.last_seen, g.last_seen),
                    versions = agg.versions,
                    environments = agg.environments,
                    suggested_severity = coalesce((SELECT severity FROM worst), g.suggested_severity),
                    severity = CASE WHEN g.triaged_by IS NULL
                                    THEN coalesce((SELECT severity FROM worst), g.severity)
                                    ELSE g.severity END,
                    severity_reason = CASE
                        WHEN g.triaged_by IS NULL
                        THEN coalesce((SELECT severity_reason FROM worst), g.severity_reason)
                        ELSE g.severity_reason END,
                    representative_trace_id = coalesce((SELECT trace_id FROM rep), g.representative_trace_id),
                    suggested_taxonomy = coalesce((SELECT taxonomy FROM rep), g.suggested_taxonomy),
                    taxonomy = CASE WHEN g.triaged_by IS NULL
                                    THEN coalesce((SELECT taxonomy FROM rep), g.taxonomy)
                                    ELSE g.taxonomy END,
                    secondary = CASE WHEN g.triaged_by IS NULL
                                     THEN coalesce((SELECT secondary FROM rep), g.secondary)
                                     ELSE g.secondary END,
                    title = coalesce((SELECT title FROM rep), g.title),
                    component = CASE WHEN (SELECT trace_id FROM rep) IS NULL
                                     THEN g.component ELSE (SELECT component FROM rep) END,
                    evidence = coalesce((SELECT evidence FROM rep), g.evidence),
                    updated_at = now()
                FROM agg WHERE g.id = %s""",
            (group_id, group_id, group_id, group_id),
        )
        return await self.group(conn, group_id)

    async def drop_if_untouched(self, conn: Conn, group_id: str) -> bool:
        """Deletes a group left without occurrences that nobody acted on (a
        trace that moved to another group, or is no longer a failure)."""
        cur = await conn.execute(
            """DELETE FROM regression_group g
               WHERE g.id = %s AND g.occurrence_count = 0 AND g.status = 'CANDIDATE'
                 AND g.triaged_by IS NULL AND g.assignee IS NULL AND g.merged_into IS NULL
                 AND NOT EXISTS (SELECT 1 FROM regression_event e
                                 WHERE e.group_id = g.id AND e.action <> 'created')
                 AND NOT EXISTS (SELECT 1 FROM regression_group m WHERE m.merged_into = g.id)""",
            (group_id,),
        )
        return cur.rowcount == 1

    async def set_status(
        self, conn: Conn, group_id: str, status: str, values: Mapping[str, Any] | None = None
    ) -> Row:
        sets = ["status = %s", "updated_at = now()"]
        params: list[Any] = [status]
        for key, value in (values or {}).items():
            sets.append(f"{key} = %s")
            params.append(value)
        row = await self._one(
            conn,
            f"UPDATE regression_group SET {', '.join(sets)} WHERE id = %s RETURNING *",  # noqa: S608 - fixed keys
            (*params, group_id),
        )
        assert row is not None  # noqa: S101 - the caller holds the locked row
        return row

    async def add_event(
        self,
        conn: Conn,
        group_id: str,
        *,
        action: str,
        actor: str,
        from_status: str | None = None,
        to_status: str | None = None,
        reason: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        await conn.execute(
            """INSERT INTO regression_event
                 (group_id, seq, action, from_status, to_status, actor, reason, detail)
               VALUES (%s, (SELECT coalesce(max(seq), 0) + 1 FROM regression_event WHERE group_id = %s),
                       %s, %s, %s, %s, %s, %s)""",
            (group_id, group_id, action, from_status, to_status, actor, reason, jsonb(dict(detail or {}))),
        )

    async def events(self, group_id: str) -> list[Row]:
        async with self.pool.connection() as conn:
            return await self._all(
                conn, "SELECT * FROM regression_event WHERE group_id = %s ORDER BY seq", (group_id,)
            )

    async def occurrences(self, group_id: str, limit: int = 50) -> list[Row]:
        async with self.pool.connection() as conn:
            return await self._all(
                conn,
                """SELECT * FROM regression_occurrence WHERE group_id = %s
                   ORDER BY started_at DESC, trace_id LIMIT %s""",
                (group_id, limit),
            )

    # ------------------------------------------------------------ fixes

    async def promoted_for(
        self, conn: Conn, organization_id: str, project_id: str, agent: str, scenarios: Sequence[str]
    ) -> list[Row]:
        """The promoted (or reopened) groups whose scenario is among
        ``scenarios``, locked."""
        if not scenarios:
            return []
        return await self._all(
            conn,
            """SELECT * FROM regression_group
               WHERE organization_id = %s AND project_id = %s AND agent_name = %s
                 AND scenario_name = ANY(%s) AND status IN ('PROMOTED', 'REOPENED') AND merged_into IS NULL
               ORDER BY id FOR UPDATE""",
            (organization_id, project_id, agent, list(scenarios)),
        )
