"""PostgreSQL access of the simulation service (``simulation`` schema)."""

from __future__ import annotations

import base64
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agenttwin_core.db import Conn, Pool, jsonb, transaction
from agenttwin_core.ids import new_id
from agenttwin_core.jobs import JobStatus, allowed_from

__all__ = ["Cursor", "Scope", "Store", "decode_cursor", "encode_cursor"]

Row = dict[str, Any]
SCHEMA = "simulation"


@dataclass(frozen=True)
class Cursor:
    ts: str
    id: str


def encode_cursor(ts: datetime | str, row_id: str) -> str:
    value = ts.isoformat() if isinstance(ts, datetime) else ts
    return base64.urlsafe_b64encode(json.dumps({"t": value, "i": row_id}).encode()).decode().rstrip("=")


def decode_cursor(token: str) -> Cursor | None:
    if not token:
        return None
    if len(token) > 512:
        raise ValueError("cursor is malformed")
    try:
        raw = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        return Cursor(ts=str(raw["t"]), id=str(raw["i"]))
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

    # ------------------------------------------------------------ twins

    async def upsert_twin(
        self, org: str, project: str, doc: Mapping[str, Any], digest: str, tool_count: int, actor: str
    ) -> tuple[Row, bool]:
        """A new version when the document changed; the latest otherwise."""
        name = str(doc["metadata"]["name"])
        async with transaction(self.pool) as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"twin/{project}/{name}",))
            latest = await self._one(
                conn,
                "SELECT * FROM twin_definition WHERE project_id = %s AND name = %s "
                "ORDER BY version DESC LIMIT 1",
                (project, name),
            )
            if latest is not None and latest["spec_hash"] == digest:
                return latest, False
            row = await self._one(
                conn,
                """INSERT INTO twin_definition (id, organization_id, project_id, name, version, document,
                       spec_hash, tool_count, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (
                    new_id(),
                    org,
                    project,
                    name,
                    (latest["version"] + 1) if latest else 1,
                    jsonb(doc),
                    digest,
                    tool_count,
                    actor,
                ),
            )
            assert row is not None  # noqa: S101 - RETURNING always yields the row
            return row, True

    async def latest_twin(self, project: str, name: str) -> Row | None:
        return await self.one(
            "SELECT * FROM twin_definition WHERE project_id = %s AND name = %s ORDER BY version DESC LIMIT 1",
            (project, name),
        )

    async def twin_by_id(self, twin_id: str) -> Row | None:
        return await self.one("SELECT * FROM twin_definition WHERE id = %s", (twin_id,))

    async def list_twins(self, scope: Scope) -> list[Row]:
        where, params = scope.clause()
        return await self.all(
            f"""SELECT DISTINCT ON (project_id, name) id, organization_id, project_id, name, version,
                    spec_hash, tool_count, created_by, created_at,
                    document->'metadata'->>'description' AS description
                FROM twin_definition WHERE {where}
                ORDER BY project_id, name, version DESC LIMIT 500""",  # noqa: S608 - fixed clauses, bound values
            params,
        )

    async def twin_versions(self, project: str, name: str) -> list[Row]:
        return await self.all(
            """SELECT id, version, spec_hash, tool_count, created_by, created_at FROM twin_definition
               WHERE project_id = %s AND name = %s ORDER BY version DESC LIMIT 100""",
            (project, name),
        )

    # ------------------------------------------------------------ scenarios

    async def upsert_scenario(
        self, conn: Conn, org: str, project: str, doc: Mapping[str, Any], digest: str, actor: str
    ) -> tuple[Row, Row, bool]:
        """(scenario, version, created) inside the caller's transaction."""
        meta, spec = doc["metadata"], doc["spec"]
        name = str(meta["name"])
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"scenario/{project}/{name}",))
        sc = await self._one(
            conn, "SELECT * FROM scenario WHERE project_id = %s AND name = %s FOR UPDATE", (project, name)
        )
        fields = (
            spec.get("agent"),
            spec.get("twin"),
            meta["severity"],
            list(meta.get("tags") or []),
            str(meta.get("source") or "manual"),
        )
        if sc is not None:
            latest = await self._one(
                conn,
                "SELECT * FROM scenario_version WHERE scenario_id = %s AND version = %s",
                (sc["id"], sc["latest_version"]),
            )
            if latest is not None and latest["spec_hash"] == digest and not sc["archived"]:
                return sc, latest, False
            version = sc["latest_version"] + 1
            sc = await self._one(
                conn,
                """UPDATE scenario SET agent = %s, twin = %s, severity = %s, tags = %s, source = %s,
                       latest_version = %s, archived = false, updated_at = now()
                   WHERE id = %s RETURNING *""",
                (*fields, version, sc["id"]),
            )
        else:
            version = 1
            sc = await self._one(
                conn,
                """INSERT INTO scenario (id, organization_id, project_id, name, agent, twin, severity, tags,
                       source, latest_version, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s) RETURNING *""",
                (new_id(), org, project, name, *fields, actor),
            )
        assert sc is not None  # noqa: S101 - RETURNING always yields the row
        ver = await self._one(
            conn,
            """INSERT INTO scenario_version (id, scenario_id, version, document, spec_hash, created_by)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
            (new_id(), sc["id"], version, jsonb(doc), digest, actor),
        )
        assert ver is not None  # noqa: S101 - RETURNING always yields the row
        return sc, ver, True

    async def list_scenarios(
        self,
        scope: Scope,
        *,
        agent: str | None = None,
        severity: str | None = None,
        tag: str | None = None,
        query: str | None = None,
        name: str | None = None,
        include_archived: bool = False,
        after: tuple[str, str] | None = None,
        limit: int = 50,
    ) -> list[Row]:
        clause, params = scope.clause("s")
        where = [clause]
        if not include_archived:
            where.append("NOT s.archived")
        if name:
            where.append("s.name = %s")
            params.append(name)
        if agent:
            where.append("(s.agent = %s OR s.agent IS NULL)")
            params.append(agent)
        if severity:
            where.append("s.severity = %s")
            params.append(severity)
        if tag:
            where.append("%s = ANY(s.tags)")
            params.append(tag)
        if query:
            where.append("(s.name ILIKE %s OR v.document->'metadata'->>'description' ILIKE %s)")
            like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params += [like, like]
        if after is not None:
            where.append("(s.name, s.project_id) > (%s, %s::uuid)")
            params += [after[0], after[1]]
        params.append(limit)
        return await self.all(
            f"""SELECT s.*, v.id AS version_id, v.spec_hash,
                    v.document->'metadata'->>'description' AS description,
                    jsonb_array_length(COALESCE(v.document->'spec'->'expectations', '[]'))
                        AS expectation_count,
                    jsonb_array_length(COALESCE(v.document->'spec'->'faults', '[]')) AS fault_count
                FROM scenario s
                JOIN scenario_version v ON v.scenario_id = s.id AND v.version = s.latest_version
                WHERE {" AND ".join(where)}
                ORDER BY s.name, s.project_id LIMIT %s""",  # noqa: S608 - fixed clauses, bound values
            params,
        )

    async def get_scenario(self, scenario_id: str, version: int | None = None) -> Row | None:
        """The scenario with the document of ``version`` (default: the latest)."""
        return await self.one(
            """SELECT s.*, v.id AS version_id, v.version AS version, v.document, v.spec_hash,
                      v.created_by AS version_created_by, v.created_at AS version_created_at
               FROM scenario s
               JOIN scenario_version v ON v.scenario_id = s.id AND v.version = COALESCE(%s, s.latest_version)
               WHERE s.id = %s""",
            (version, scenario_id),
        )

    async def scenario_versions(self, scenario_id: str) -> list[Row]:
        return await self.all(
            """SELECT id, version, spec_hash, created_by, created_at FROM scenario_version
               WHERE scenario_id = %s ORDER BY version DESC LIMIT 100""",
            (scenario_id,),
        )

    async def archive_scenario(self, scenario_id: str) -> Row | None:
        return await self.one(
            "UPDATE scenario SET archived = true, updated_at = now() WHERE id = %s RETURNING *",
            (scenario_id,),
        )

    async def scenarios_for_run(
        self, project: str, agent: str, names: Sequence[str] | None, tags: Sequence[str] | None
    ) -> list[Row]:
        where = ["s.project_id = %s", "NOT s.archived", "(s.agent = %s OR s.agent IS NULL)"]
        params: list[Any] = [project, agent]
        if names:
            where.append("s.name = ANY(%s)")
            params.append(list(names))
        if tags:
            where.append("s.tags && %s")
            params.append(list(tags))
        return await self.all(
            f"""SELECT s.id, s.name, s.severity, s.twin, v.id AS version_id, v.document, v.spec_hash
                FROM scenario s
                JOIN scenario_version v ON v.scenario_id = s.id AND v.version = s.latest_version
                WHERE {" AND ".join(where)}
                ORDER BY CASE s.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                                         WHEN 'medium' THEN 2 ELSE 3 END,
                         s.name""",  # noqa: S608 - fixed clauses, bound values
            params,
        )

    # ------------------------------------------------------------ runs

    async def insert_run(self, conn: Conn, run: Mapping[str, Any], cases: Sequence[Mapping[str, Any]]) -> Row:
        row = await self._one(
            conn,
            """INSERT INTO simulation_run (id, organization_id, project_id, agent_name, agent_version,
                   agent_version_id, side, eval_run_id, release_id, status, requested_by, case_count, pinning)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'QUEUED', %s, %s, %s) RETURNING *""",
            (
                run["id"],
                run["organization_id"],
                run["project_id"],
                run["agent_name"],
                run["agent_version"],
                run.get("agent_version_id"),
                run.get("side", "SINGLE"),
                run.get("eval_run_id"),
                run.get("release_id"),
                run["requested_by"],
                len(cases),
                jsonb(run.get("pinning") or {}),
            ),
        )
        assert row is not None  # noqa: S101 - RETURNING always yields the row
        for c in cases:
            await conn.execute(
                """INSERT INTO simulation_case (id, run_id, position, scenario_id, scenario_version_id,
                       scenario_name, severity, twin_definition_id, status, seed, tenant)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'PENDING', %s, %s)""",
                (
                    c["id"],
                    run["id"],
                    c["position"],
                    c["scenario_id"],
                    c["scenario_version_id"],
                    c["scenario_name"],
                    c["severity"],
                    c["twin_definition_id"],
                    c["seed"],
                    c.get("tenant"),
                ),
            )
        await self.add_transition(conn, str(run["id"]), None, JobStatus.QUEUED, "requested")
        return row

    # ------------------------------------------------------------ pairs

    async def claim_pair(self, conn: Conn, pair: Mapping[str, Any]) -> bool:
        """Writes an evaluation run's pair row (its runs follow in the same
        transaction; the foreign keys are checked at commit). False when the
        evaluation run already has a pair: a concurrent claim waits on the key
        until the first transaction ends."""
        row = await self._one(
            conn,
            """INSERT INTO simulation_pair (eval_run_id, organization_id, project_id, baseline_run_id,
                   candidate_run_id, request_sha256, requested_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (eval_run_id) DO NOTHING RETURNING eval_run_id""",
            (
                pair["eval_run_id"],
                pair["organization_id"],
                pair["project_id"],
                pair["baseline_run_id"],
                pair["candidate_run_id"],
                pair["request_sha256"],
                pair["requested_by"],
            ),
        )
        return row is not None

    async def get_pair(self, eval_run_id: str) -> Row | None:
        return await self.one("SELECT * FROM simulation_pair WHERE eval_run_id = %s", (eval_run_id,))

    async def add_transition(
        self, conn: Conn, run_id: str, from_status: str | None, to_status: str, reason: str | None
    ) -> None:
        await conn.execute(
            """INSERT INTO simulation_run_transition (run_id, seq, from_status, to_status, reason)
               SELECT %s, COALESCE(MAX(seq), 0) + 1, %s, %s, %s
               FROM simulation_run_transition WHERE run_id = %s""",
            (run_id, from_status, str(to_status), reason, run_id),
        )

    async def transition(
        self,
        conn: Conn,
        run_id: str,
        to: JobStatus,
        reason: str | None = None,
        *,
        owner: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> Row | None:
        """Moves a run to ``to`` if the state machine allows it from its current
        status (and, with ``owner``, only while that worker holds the lease)."""
        sets = ["status = %s", "updated_at = now()"]
        params: list[Any] = [str(to)]
        if to.terminal:
            sets += ["finished_at = now()", "lease_owner = NULL", "lease_expires_at = NULL"]
        for key, value in (extra or {}).items():
            if key not in ("error", "pinning", "started_at"):
                raise ValueError(f"unsupported column {key}")
            sets.append(f"{key} = %s")
            params.append(jsonb(value) if key == "pinning" else value)
        where = "id = %s AND status = ANY(%s)"
        params += [run_id, [str(s) for s in allowed_from(to)]]
        if owner is not None:
            where += " AND lease_owner = %s"
            params.append(owner)
        prev = await self._one(conn, "SELECT status FROM simulation_run WHERE id = %s FOR UPDATE", (run_id,))
        if prev is None:
            return None
        row = await self._one(
            conn,
            f"UPDATE simulation_run SET {', '.join(sets)} WHERE {where} RETURNING *",  # noqa: S608 - fixed columns
            params,
        )
        if row is not None:
            await self.add_transition(conn, run_id, prev["status"], str(to), reason)
        return row

    async def claim_next_run(self, owner: str, lease_s: float) -> Row | None:
        async with transaction(self.pool) as conn:
            row = await self._one(
                conn,
                """UPDATE simulation_run SET status = 'PREPARING', lease_owner = %s,
                       lease_expires_at = now() + make_interval(secs => %s), attempts = attempts + 1,
                       started_at = COALESCE(started_at, now()), updated_at = now()
                   WHERE id = (SELECT id FROM simulation_run WHERE status = 'QUEUED'
                               ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
                   RETURNING *""",
                (owner, lease_s),
            )
            if row is not None:
                await self.add_transition(conn, row["id"], "QUEUED", "PREPARING", f"claimed by {owner}")
            return row

    async def renew_lease(self, run_id: str, owner: str, lease_s: float) -> bool:
        row = await self.one(
            """UPDATE simulation_run
               SET lease_expires_at = now() + make_interval(secs => %s), updated_at = now()
               WHERE id = %s AND lease_owner = %s AND status IN ('PREPARING', 'RUNNING', 'EVALUATING')
               RETURNING id""",
            (lease_s, run_id, owner),
        )
        return row is not None

    async def recover_expired(
        self, max_attempts: int, on_terminal: Callable[[Conn, Row], Awaitable[None]] | None = None
    ) -> list[tuple[str, str]]:
        """Runs whose worker lost its lease go back to the queue (or fail after
        ``max_attempts``); their in-flight cases restart from a fresh twin. A
        run whose cancellation was requested is cancelled instead of being
        retried. ``on_terminal`` runs in the same transaction for runs that
        ended (to write their completion event)."""
        out: list[tuple[str, str]] = []
        async with transaction(self.pool) as conn:
            rows = await self._all(
                conn,
                """SELECT id, status, attempts, cancel_requested, organization_id, project_id,
                          eval_run_id, side
                   FROM simulation_run
                   WHERE status IN ('PREPARING', 'RUNNING', 'EVALUATING') AND lease_expires_at < now()
                   ORDER BY lease_expires_at LIMIT 20 FOR UPDATE SKIP LOCKED""",
            )
            for r in rows:
                await conn.execute(
                    """UPDATE simulation_case SET status = 'PENDING', token_hash = NULL, twin_state = NULL,
                           call_count = 0, started_at = NULL, updated_at = now()
                       WHERE run_id = %s AND status = 'RUNNING'""",
                    (r["id"],),
                )
                await conn.execute(
                    "DELETE FROM simulation_step s USING simulation_case c "
                    "WHERE s.case_id = c.id AND c.run_id = %s AND c.status = 'PENDING'",
                    (r["id"],),
                )
                if r["cancel_requested"] and str(r["status"]) in allowed_from(JobStatus.CANCELLED):
                    to, reason = JobStatus.CANCELLED, "cancelled on request (the worker lease expired)"
                    await self.cancel_pending_cases(conn, r["id"], "The run was cancelled.")
                elif r["attempts"] >= max_attempts:
                    to, reason = JobStatus.FAILED, f"worker lease expired {r['attempts']} times"
                    await self.cancel_pending_cases(
                        conn, r["id"], "The run failed: its worker stopped renewing the lease."
                    )
                else:
                    to, reason = JobStatus.QUEUED, "worker lease expired"
                row = await self.transition(
                    conn, r["id"], to, reason, extra={"error": reason} if to is JobStatus.FAILED else None
                )
                if row is not None:
                    await conn.execute(
                        "UPDATE simulation_run SET lease_owner = NULL, lease_expires_at = NULL WHERE id = %s",
                        (r["id"],),
                    )
                    if to.terminal:
                        counted = await self.refresh_counts(conn, r["id"])
                        if on_terminal is not None:
                            await on_terminal(conn, counted or row)
                    out.append((str(r["id"]), str(to)))
        return out

    async def request_cancel(
        self, run_id: str, actor: str, on_terminal: Callable[[Conn, Row], Awaitable[None]] | None = None
    ) -> Row | None:
        """Cancels a queued run at once (``on_terminal`` runs in the same
        transaction); a run being executed is flagged and stopped by its worker."""
        async with transaction(self.pool) as conn:
            run = await self._one(conn, "SELECT * FROM simulation_run WHERE id = %s FOR UPDATE", (run_id,))
            if run is None:
                return None
            if run["status"] == "QUEUED":
                # No worker holds it: cancel at once.
                await conn.execute(
                    "UPDATE simulation_run SET cancel_requested = true WHERE id = %s", (run_id,)
                )
                await self.cancel_pending_cases(conn, run_id, f"The run was cancelled by {actor}.")
                await self.refresh_counts(conn, run_id)
                row = await self.transition(conn, run_id, JobStatus.CANCELLED, f"cancelled by {actor}")
                if row is not None and on_terminal is not None:
                    await on_terminal(conn, row)
                return row
            if not JobStatus(run["status"]).terminal:
                return await self._one(
                    conn,
                    "UPDATE simulation_run SET cancel_requested = true, updated_at = now() "
                    "WHERE id = %s RETURNING *",
                    (run_id,),
                )
            return run

    async def list_runs(
        self,
        scope: Scope,
        *,
        agent: str | None = None,
        status: str | None = None,
        eval_run_id: str | None = None,
        cursor: Cursor | None = None,
        limit: int = 50,
    ) -> list[Row]:
        clause, params = scope.clause()
        where = [clause]
        if agent:
            where.append("agent_name = %s")
            params.append(agent)
        if status:
            where.append("status = %s")
            params.append(status)
        if eval_run_id:
            where.append("eval_run_id = %s")
            params.append(eval_run_id)
        if cursor is not None:
            where.append("(created_at, id) < (%s::timestamptz, %s::uuid)")
            params += [cursor.ts, cursor.id]
        params.append(limit)
        return await self.all(
            f"""SELECT * FROM simulation_run WHERE {" AND ".join(where)}
                ORDER BY created_at DESC, id DESC LIMIT %s""",  # noqa: S608 - fixed clauses, bound values
            params,
        )

    async def get_run(self, run_id: str) -> Row | None:
        return await self.one("SELECT * FROM simulation_run WHERE id = %s", (run_id,))

    async def run_transitions(self, run_id: str) -> list[Row]:
        return await self.all(
            "SELECT seq, from_status, to_status, reason, at FROM simulation_run_transition "
            "WHERE run_id = %s ORDER BY seq",
            (run_id,),
        )

    async def run_cases(self, run_id: str) -> list[Row]:
        return await self.all(
            """SELECT id, run_id, position, scenario_id, scenario_version_id, scenario_name, severity,
                   twin_definition_id, status, seed, tenant, call_count, trace_id, reason, error, latency_ms,
                   verdict, outcome_status, started_at, finished_at, updated_at
               FROM simulation_case WHERE run_id = %s ORDER BY position""",
            (run_id,),
        )

    async def get_case(self, run_id: str, case_id: str) -> Row | None:
        return await self.one(
            """SELECT c.*, v.document AS scenario_document FROM simulation_case c
               JOIN scenario_version v ON v.id = c.scenario_version_id
               WHERE c.run_id = %s AND c.id = %s""",
            (run_id, case_id),
        )

    async def case_steps(self, case_id: str) -> list[Row]:
        return await self.all(
            "SELECT seq, kind, tool, record, latency_ms, created_at FROM simulation_step "
            "WHERE case_id = %s ORDER BY seq",
            (case_id,),
        )

    # ------------------------------------------------------------ worker: cases

    async def pending_cases(self, run_id: str) -> list[Row]:
        return await self.all(
            """SELECT c.*, v.document AS scenario_document, t.document AS twin_document
               FROM simulation_case c
               JOIN scenario_version v ON v.id = c.scenario_version_id
               JOIN twin_definition t ON t.id = c.twin_definition_id
               WHERE c.run_id = %s AND c.status IN ('PENDING', 'RUNNING') ORDER BY c.position""",
            (run_id,),
        )

    async def start_case(self, case_id: str, token_hash: bytes, twin_state: Mapping[str, Any]) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """UPDATE simulation_case SET status = 'RUNNING', token_hash = %s, twin_state = %s,
                       call_count = 0, started_at = now(), updated_at = now()
                   WHERE id = %s""",
                (token_hash, jsonb(twin_state), case_id),
            )
            await conn.execute("DELETE FROM simulation_step WHERE case_id = %s", (case_id,))

    async def close_case(self, case_id: str) -> Row | None:
        """Revokes the case's twin credential and returns its final twin state."""
        return await self.one(
            """UPDATE simulation_case SET token_hash = NULL, updated_at = now() WHERE id = %s
               RETURNING twin_state, call_count""",
            (case_id,),
        )

    async def finish_case(self, conn: Conn, case_id: str, fields: Mapping[str, Any]) -> None:
        allowed = {
            "status",
            "agent_result",
            "trace_id",
            "verdict",
            "results",
            "state_diff",
            "reason",
            "error",
            "latency_ms",
            "outcome_status",
        }
        sets, params = ["finished_at = now()", "updated_at = now()", "token_hash = NULL"], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"unsupported column {key}")
            sets.append(f"{key} = %s")
            params.append(
                jsonb(value) if key in ("agent_result", "verdict", "results", "state_diff") else value
            )
        if fields.get("outcome_status") == "pending":
            sets.append("outcome_next_at = now()")
        params.append(case_id)
        await conn.execute(
            f"UPDATE simulation_case SET {', '.join(sets)} WHERE id = %s",  # noqa: S608 - fixed columns
            params,
        )

    async def refresh_counts(self, conn: Conn, run_id: str) -> Row | None:
        return await self._one(
            conn,
            """UPDATE simulation_run r SET
                   passed = s.passed, failed = s.failed, errored = s.errored, cancelled = s.cancelled,
                   critical_failures = s.critical, updated_at = now()
               FROM (SELECT count(*) FILTER (WHERE status = 'PASSED') AS passed,
                            count(*) FILTER (WHERE status = 'FAILED') AS failed,
                            count(*) FILTER (WHERE status = 'ERRORED') AS errored,
                            count(*) FILTER (WHERE status = 'CANCELLED') AS cancelled,
                            count(*) FILTER (WHERE status = 'FAILED' AND severity = 'critical') AS critical
                     FROM simulation_case WHERE run_id = %s) s
               WHERE r.id = %s RETURNING r.*""",
            (run_id, run_id),
        )

    async def cancel_pending_cases(
        self, conn: Conn, run_id: str, reason: str = "The run was cancelled."
    ) -> None:
        await conn.execute(
            """UPDATE simulation_case SET status = 'CANCELLED', token_hash = NULL, finished_at = now(),
                   updated_at = now(), reason = %s
               WHERE run_id = %s AND status IN ('PENDING', 'RUNNING')""",
            (reason, run_id),
        )

    async def queue_depth(self) -> int:
        row = await self.one("SELECT count(*) AS n FROM simulation_run WHERE status = 'QUEUED'")
        return int(row["n"]) if row else 0

    async def cancel_requested(self, run_id: str) -> bool:
        row = await self.one("SELECT cancel_requested FROM simulation_run WHERE id = %s", (run_id,))
        return bool(row and row["cancel_requested"])

    async def lock_cancel_requested(self, conn: Conn, run_id: str) -> bool:
        """Locks the run row (``request_cancel`` takes the same lock), so the
        answer holds until ``conn`` commits."""
        row = await self._one(
            conn, "SELECT cancel_requested FROM simulation_run WHERE id = %s FOR UPDATE", (run_id,)
        )
        return bool(row and row["cancel_requested"])

    # ------------------------------------------------------------ twin endpoint

    async def lock_case_by_token(self, conn: Conn, token_hash: bytes) -> Row | None:
        return await self._one(
            conn,
            """SELECT c.id, c.run_id, c.status, c.seed, c.tenant, c.twin_state, c.call_count,
                      c.twin_definition_id, c.scenario_version_id, r.organization_id, r.project_id,
                      r.status AS run_status, r.cancel_requested, r.agent_name
               FROM simulation_case c JOIN simulation_run r ON r.id = c.run_id
               WHERE c.token_hash = %s FOR UPDATE OF c""",
            (token_hash,),
        )

    async def save_twin_call(
        self,
        conn: Conn,
        case_id: str,
        twin_state: Mapping[str, Any],
        steps: Sequence[tuple[int, str, str | None, Mapping[str, Any], float | None]],
    ) -> None:
        await conn.execute(
            "UPDATE simulation_case SET twin_state = %s, call_count = call_count + %s, updated_at = now() "
            "WHERE id = %s",
            (jsonb(twin_state), sum(1 for s in steps if s[1] == "tool_call"), case_id),
        )
        for seq, kind, tool, record, latency in steps:
            await conn.execute(
                """INSERT INTO simulation_step (case_id, seq, kind, tool, record, latency_ms)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (case_id, seq, kind, tool, jsonb(record), latency),
            )

    async def scenario_document(self, version_id: str) -> Row | None:
        return await self.one("SELECT document FROM scenario_version WHERE id = %s", (version_id,))

    # ------------------------------------------------------------ verified outcomes

    async def due_outcomes(self, limit: int = 20) -> list[Row]:
        return await self.all(
            """SELECT c.id, c.run_id, c.trace_id, c.status, c.verdict, c.results, c.agent_result,
                      c.outcome_attempts, c.scenario_name, r.organization_id, r.project_id
               FROM simulation_case c JOIN simulation_run r ON r.id = c.run_id
               WHERE c.outcome_status = 'pending' AND c.outcome_next_at <= now()
               ORDER BY c.outcome_next_at LIMIT %s""",
            (limit,),
        )

    async def mark_outcome(self, case_id: str, status: str, retry_in_s: float | None = None) -> None:
        if retry_in_s is None:
            await self.one(
                "UPDATE simulation_case SET outcome_status = %s, outcome_attempts = outcome_attempts + 1, "
                "outcome_next_at = NULL, updated_at = now() WHERE id = %s RETURNING id",
                (status, case_id),
            )
        else:
            await self.one(
                "UPDATE simulation_case SET outcome_attempts = outcome_attempts + 1, "
                "outcome_next_at = now() + make_interval(secs => %s), updated_at = now() "
                "WHERE id = %s RETURNING id",
                (retry_in_s, case_id),
            )
