"""PostgreSQL access of the evaluation service (``evaluation`` schema)."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agenttwin_core.db import Conn, Pool, jsonb, transaction
from agenttwin_core.jobs import JobStatus, allowed_from
from agenttwin_evaluation.judges import JudgeVerdict

__all__ = ["SCHEMA", "Cursor", "JudgmentCache", "Row", "Scope", "Store", "decode_cursor", "encode_cursor"]

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

    async def dataset_named(self, conn: Conn, project_id: str, name: str) -> Row | None:
        """A project's dataset by name, locked."""
        return await self._one(
            conn, "SELECT * FROM dataset WHERE project_id = %s AND name = %s FOR UPDATE", (project_id, name)
        )

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

    # ------------------------------------------------------------ evaluation runs

    _RUN_COLUMNS = frozenset(
        {
            "error",
            "pinning",
            "baseline_run_id",
            "candidate_run_id",
            "case_count",
            "next_check_at",
            "wait_deadline",
            "judge",
            "budget",
            "summary",
            "new_critical_failures",
            "regressed",
            "improved",
            "unchanged",
            "incomplete",
        }
    )
    _JSON_COLUMNS = frozenset({"pinning", "judge", "budget", "summary"})

    async def insert_eval_run(self, conn: Conn, run: Mapping[str, Any]) -> Row:
        row = await self._insert_eval_run(conn, run, "requested")
        assert row is not None  # noqa: S101 - no release evaluation, nothing to conflict with
        return row

    async def insert_release_run(self, conn: Conn, run: Mapping[str, Any]) -> Row | None:
        """The run of one release evaluation, created once however often it is
        asked for: None when that release evaluation already has its run."""
        return await self._insert_eval_run(
            conn, run, f"requested by release evaluation {run['release_evaluation_id']}"
        )

    async def _insert_eval_run(self, conn: Conn, run: Mapping[str, Any], reason: str) -> Row | None:
        row = await self._one(
            conn,
            """INSERT INTO eval_run (id, organization_id, project_id, agent_name, baseline_version,
                   candidate_version, dataset_id, dataset_version, selection, seed, release_id, status,
                   requested_by, release_evaluation_id, max_judge_cost_usd)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'QUEUED', %s, %s, %s)
               ON CONFLICT (organization_id, release_evaluation_id) WHERE release_evaluation_id IS NOT NULL
               DO NOTHING
               RETURNING *""",
            (
                run["id"],
                run["organization_id"],
                run["project_id"],
                run["agent_name"],
                run["baseline_version"],
                run["candidate_version"],
                run.get("dataset_id"),
                run.get("dataset_version"),
                jsonb(run["selection"]),
                run.get("seed"),
                run.get("release_id"),
                run["requested_by"],
                run.get("release_evaluation_id"),
                run.get("max_judge_cost_usd"),
            ),
        )
        if row is not None:
            await self.add_transition(conn, str(run["id"]), None, "QUEUED", reason)
        return row

    async def eval_run_of_release(self, organization_id: str, release_evaluation_id: str) -> Row | None:
        return await self.one(
            "SELECT * FROM eval_run WHERE organization_id = %s AND release_evaluation_id = %s",
            (organization_id, release_evaluation_id),
        )

    async def add_transition(
        self, conn: Conn, run_id: str, from_status: str | None, to_status: str, reason: str | None
    ) -> None:
        await conn.execute(
            """INSERT INTO eval_run_transition (eval_run_id, seq, from_status, to_status, reason)
               VALUES (%s, (SELECT COALESCE(max(seq), 0) + 1 FROM eval_run_transition WHERE eval_run_id = %s),
                       %s, %s, %s)""",
            (run_id, run_id, from_status, to_status, reason),
        )

    async def get_eval_run(self, run_id: str) -> Row | None:
        return await self.one("SELECT * FROM eval_run WHERE id = %s", (run_id,))

    async def eval_run_transitions(self, run_id: str) -> list[Row]:
        return await self.all(
            """SELECT from_status, to_status, reason, at FROM eval_run_transition
               WHERE eval_run_id = %s ORDER BY seq""",
            (run_id,),
        )

    async def list_eval_runs(
        self,
        scope: Scope,
        *,
        status: str | None = None,
        agent: str | None = None,
        dataset_id: str | None = None,
        release_evaluation_id: str | None = None,
        after: Cursor | None = None,
        limit: int = 50,
    ) -> list[Row]:
        clause, params = scope.clause()
        where = [clause]
        for column, value in (
            ("status", status),
            ("agent_name", agent),
            ("dataset_id", dataset_id),
            ("release_evaluation_id", release_evaluation_id),
        ):
            if value is not None:
                where.append(f"{column} = %s")
                params.append(value)
        if after is not None:
            where.append("(created_at, id) < (%s::timestamptz, %s::uuid)")
            params += [after.key, after.id]
        params.append(limit)
        return await self.all(
            f"""SELECT * FROM eval_run WHERE {" AND ".join(where)}
                ORDER BY created_at DESC, id DESC LIMIT %s""",  # noqa: S608 - fixed clauses, bound values
            params,
        )

    async def transition(
        self,
        conn: Conn,
        run_id: str,
        to: JobStatus,
        reason: str | None = None,
        *,
        owner: str | None = None,
        lease: tuple[str, float] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> Row | None:
        """Moves a run to ``to`` if the state machine allows it from its
        current status (and, with ``owner``, only while that worker holds the
        lease). ``lease`` (owner, seconds) gives the run to a worker;
        otherwise the lease is released — a RUNNING run waits without one."""
        sets = ["status = %s", "updated_at = now()"]
        params: list[Any] = [str(to)]
        if to.terminal:
            sets += ["finished_at = now()", "next_check_at = NULL"]
        if lease is not None:
            sets += ["lease_owner = %s", "lease_expires_at = now() + make_interval(secs => %s)"]
            params += [lease[0], lease[1]]
        else:
            sets += ["lease_owner = NULL", "lease_expires_at = NULL"]
        for key, value in (extra or {}).items():
            if key not in self._RUN_COLUMNS:
                raise ValueError(f"unsupported column {key}")
            sets.append(f"{key} = %s")
            params.append(jsonb(value) if key in self._JSON_COLUMNS else value)
        where = "id = %s AND status = ANY(%s)"
        params += [run_id, allowed_from(to)]
        if owner is not None:
            where += " AND lease_owner = %s"
            params.append(owner)
        prev = await self._one(conn, "SELECT status FROM eval_run WHERE id = %s FOR UPDATE", (run_id,))
        if prev is None:
            return None
        row = await self._one(
            conn,
            f"UPDATE eval_run SET {', '.join(sets)} WHERE {where} RETURNING *",  # noqa: S608 - fixed columns
            params,
        )
        if row is not None:
            await self.add_transition(conn, run_id, str(prev["status"]), str(to), reason)
        return row

    async def claim_next_eval_run(self, owner: str, lease_s: float) -> Row | None:
        async with transaction(self.pool) as conn:
            row = await self._one(
                conn,
                """UPDATE eval_run SET status = 'PREPARING', lease_owner = %s,
                       lease_expires_at = now() + make_interval(secs => %s), attempts = attempts + 1,
                       started_at = COALESCE(started_at, now()), updated_at = now()
                   WHERE id = (SELECT id FROM eval_run WHERE status = 'QUEUED'
                               ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
                   RETURNING *""",
                (owner, lease_s),
            )
            if row is not None:
                await self.add_transition(conn, str(row["id"]), "QUEUED", "PREPARING", f"claimed by {owner}")
            return row

    async def renew_lease(self, run_id: str, owner: str, lease_s: float) -> bool:
        row = await self.one(
            """UPDATE eval_run SET lease_expires_at = now() + make_interval(secs => %s), updated_at = now()
               WHERE id = %s AND lease_owner = %s AND status IN ('PREPARING', 'EVALUATING') RETURNING id""",
            (lease_s, run_id, owner),
        )
        return row is not None

    async def due_waiting(self, poll_s: float, limit: int = 10) -> list[Row]:
        """RUNNING runs whose check is due, each moved to its next check (so
        concurrent workers take different runs)."""
        return await self.all(
            """UPDATE eval_run SET next_check_at = now() + make_interval(secs => %s)
               WHERE id IN (SELECT id FROM eval_run
                            WHERE status = 'RUNNING' AND next_check_at <= now()
                            ORDER BY next_check_at LIMIT %s FOR UPDATE SKIP LOCKED)
               RETURNING *""",
            (poll_s, limit),
        )

    async def wake(self, conn: Conn, *, eval_run_id: str | None, simulation_run_id: str) -> Row | None:
        """Makes the run waiting for this simulation due now."""
        return await self._one(
            conn,
            """UPDATE eval_run SET next_check_at = now()
               WHERE status = 'RUNNING' AND (id = %s OR baseline_run_id = %s OR candidate_run_id = %s)
               RETURNING id""",
            (eval_run_id, simulation_run_id, simulation_run_id),
        )

    async def request_cancel(self, conn: Conn, run_id: str) -> Row | None:
        return await self._one(
            conn,
            """UPDATE eval_run SET cancel_requested = true, next_check_at = now(), updated_at = now()
               WHERE id = %s RETURNING *""",
            (run_id,),
        )

    async def active_run_counts(self) -> dict[str, int]:
        """Unfinished runs by status (QUEUED is the queue a worker claims from)."""
        rows = await self.all(
            """SELECT status, count(*) AS n FROM eval_run
               WHERE status IN ('QUEUED', 'PREPARING', 'RUNNING', 'EVALUATING') GROUP BY status"""
        )
        return {str(r["status"]): int(r["n"]) for r in rows}

    async def expired_leases(self, conn: Conn, limit: int = 20) -> list[Row]:
        return await self._all(
            conn,
            """SELECT * FROM eval_run
               WHERE status IN ('PREPARING', 'EVALUATING') AND lease_expires_at < now()
               ORDER BY lease_expires_at LIMIT %s FOR UPDATE SKIP LOCKED""",
            (limit,),
        )

    async def save_case_results(self, conn: Conn, run_id: str, cases: Sequence[Mapping[str, Any]]) -> None:
        """The run's case results, replacing any from an earlier attempt."""
        await conn.execute("DELETE FROM eval_case_result WHERE eval_run_id = %s", (run_id,))
        for c in cases:
            await conn.execute(
                """INSERT INTO eval_case_result (eval_run_id, position, scenario_name, severity, tags,
                       classification, baseline_case_id, candidate_case_id, sides, comparison, needs_review)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    run_id,
                    c["position"],
                    c["scenario_name"],
                    c["severity"],
                    list(c["tags"]),
                    c["classification"],
                    c.get("baseline_case_id"),
                    c.get("candidate_case_id"),
                    jsonb(c["sides"]),
                    jsonb(c["comparison"]),
                    list(c.get("needs_review") or []),
                ),
            )

    async def case_results(self, run_id: str) -> list[Row]:
        return await self.all(
            """SELECT position, scenario_name, severity, tags, classification, reviewed,
                      comparison->'reason' AS reason, comparison->'baseline' AS baseline,
                      comparison->'candidate' AS candidate
               FROM eval_case_result WHERE eval_run_id = %s ORDER BY position""",
            (run_id,),
        )

    async def case_result(self, run_id: str, scenario: str) -> Row | None:
        return await self.one(
            "SELECT * FROM eval_case_result WHERE eval_run_id = %s AND scenario_name = %s", (run_id, scenario)
        )

    # ------------------------------------------------------------ judge verdicts

    async def get_judgment(self, key: str) -> Row | None:
        return await self.one("SELECT verdict FROM judgment WHERE cache_key = %s", (key,))

    async def put_judgment(self, key: str, verdict: Mapping[str, Any], judge: Mapping[str, Any]) -> None:
        await self.one(
            """INSERT INTO judgment (cache_key, verdict, judge) VALUES (%s, %s, %s)
               ON CONFLICT (cache_key) DO NOTHING RETURNING cache_key""",
            (key, jsonb(dict(verdict)), jsonb(dict(judge))),
        )

    # ------------------------------------------------------------ reviews

    async def lock_eval_run(self, conn: Conn, run_id: str) -> Row | None:
        return await self._one(conn, "SELECT * FROM eval_run WHERE id = %s FOR UPDATE", (run_id,))

    async def case_rows_in(self, conn: Conn, run_id: str) -> list[Row]:
        return await self._all(
            conn, "SELECT * FROM eval_case_result WHERE eval_run_id = %s ORDER BY position", (run_id,)
        )

    async def insert_review(self, conn: Conn, review: Mapping[str, Any]) -> Row:
        row = await self._one(
            conn,
            """INSERT INTO human_review (id, eval_run_id, organization_id, project_id, scenario_name, side,
                   expectation_id, original_status, original_label, status, note, reviewer)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            tuple(
                review[k]
                for k in (
                    "id",
                    "eval_run_id",
                    "organization_id",
                    "project_id",
                    "scenario_name",
                    "side",
                    "expectation_id",
                    "original_status",
                    "original_label",
                    "status",
                    "note",
                    "reviewer",
                )
            ),
        )
        assert row is not None  # noqa: S101 - INSERT … RETURNING
        return row

    async def reviews_in(self, conn: Conn, run_id: str) -> list[Row]:
        return await self._all(
            conn, "SELECT * FROM human_review WHERE eval_run_id = %s ORDER BY created_at, id", (run_id,)
        )

    async def reviews_of(self, run_id: str, scenario: str) -> list[Row]:
        return await self.all(
            """SELECT * FROM human_review WHERE eval_run_id = %s AND scenario_name = %s
               ORDER BY created_at, id""",
            (run_id, scenario),
        )

    async def update_case(self, conn: Conn, run_id: str, position: int, values: Mapping[str, Any]) -> None:
        await conn.execute(
            """UPDATE eval_case_result SET classification = %s, comparison = %s, sides = %s,
                   reviewed = reviewed OR %s, updated_at = now()
               WHERE eval_run_id = %s AND position = %s""",
            (
                values["classification"],
                jsonb(values["comparison"]),
                jsonb(values["sides"]),
                bool(values.get("reviewed")),
                run_id,
                position,
            ),
        )

    async def update_run_summary(self, conn: Conn, run_id: str, summary: Mapping[str, Any]) -> Row | None:
        counts = summary["counts"]
        return await self._one(
            conn,
            """UPDATE eval_run SET summary = %s, new_critical_failures = %s, regressed = %s, improved = %s,
                   unchanged = %s, incomplete = %s, updated_at = now()
               WHERE id = %s RETURNING *""",
            (
                jsonb(dict(summary)),
                counts["NEW_CRITICAL_FAILURE"],
                counts["REGRESSED"],
                counts["IMPROVED"],
                counts["UNCHANGED"],
                counts["INCOMPLETE"],
                run_id,
            ),
        )

    async def review_queue(self, scope: Scope, *, after: Cursor | None = None, limit: int = 50) -> list[Row]:
        """Cases of completed runs with an expectation a person should look at
        and has not reviewed yet, newest run first."""
        clause, params = scope.clause("r")
        where = [clause, "r.status = 'COMPLETED'", "cardinality(c.needs_review) > 0"]
        if after is not None:
            where.append("(r.finished_at, r.id, c.position) < (%s::timestamptz, %s::uuid, %s)")
            key, _, position = after.key.rpartition("|")
            params += [key, after.id, int(position)]
        params.append(limit)
        return await self.all(
            f"""SELECT c.eval_run_id, c.position, c.scenario_name, c.severity, c.classification,
                       c.needs_review, c.sides, r.agent_name, r.baseline_version, r.candidate_version,
                       r.project_id, r.finished_at,
                       ARRAY(SELECT h.side || ':' || h.expectation_id FROM human_review h
                             WHERE h.eval_run_id = c.eval_run_id
                               AND h.scenario_name = c.scenario_name) AS done
                FROM eval_case_result c JOIN eval_run r ON r.id = c.eval_run_id
                WHERE {" AND ".join(where)}
                  AND EXISTS (SELECT 1 FROM unnest(c.needs_review) k
                              WHERE k NOT IN (SELECT h.side || ':' || h.expectation_id FROM human_review h
                                              WHERE h.eval_run_id = c.eval_run_id
                                                AND h.scenario_name = c.scenario_name))
                ORDER BY r.finished_at DESC, r.id DESC, c.position DESC LIMIT %s""",  # noqa: S608 - fixed clauses
            params,
        )

    # ------------------------------------------------------------ judge calibrations

    async def insert_calibration(self, conn: Conn, row: Mapping[str, Any]) -> Row:
        out = await self._one(
            conn,
            """INSERT INTO judge_calibration (id, organization_id, project_id, criterion, status, examples,
                   example_count, examples_sha256, requested_by)
               VALUES (%s, %s, %s, %s, 'QUEUED', %s, %s, %s, %s) RETURNING *""",
            (
                row["id"],
                row["organization_id"],
                row["project_id"],
                row["criterion"],
                jsonb(list(row["examples"])),
                len(row["examples"]),
                row["examples_sha256"],
                row["requested_by"],
            ),
        )
        assert out is not None  # noqa: S101 - INSERT … RETURNING
        return out

    async def get_calibration(self, calibration_id: str) -> Row | None:
        return await self.one("SELECT * FROM judge_calibration WHERE id = %s", (calibration_id,))

    async def list_calibrations(
        self, scope: Scope, *, criterion: str | None = None, after: Cursor | None = None, limit: int = 50
    ) -> list[Row]:
        clause, params = scope.clause()
        where = [clause]
        if criterion is not None:
            where.append("criterion = %s")
            params.append(criterion)
        if after is not None:
            where.append("(created_at, id) < (%s::timestamptz, %s::uuid)")
            params += [after.key, after.id]
        params.append(limit)
        return await self.all(
            f"""SELECT * FROM judge_calibration WHERE {" AND ".join(where)}
                ORDER BY created_at DESC, id DESC LIMIT %s""",  # noqa: S608 - fixed clauses, bound values
            params,
        )

    async def latest_calibrations(self, org: str, project: str, judge: Mapping[str, str]) -> dict[str, Row]:
        """The latest completed calibration of each criterion for this judge
        (same provider, model and prompt)."""
        rows = await self.all(
            """SELECT DISTINCT ON (criterion) * FROM judge_calibration
               WHERE organization_id = %s AND project_id = %s AND status = 'COMPLETED'
                 AND judge->>'provider' = %s AND judge->>'model' = %s AND judge->>'prompt_sha256' = %s
               ORDER BY criterion, finished_at DESC, id DESC""",
            (org, project, judge["provider"], judge["model"], judge["prompt_sha256"]),
        )
        return {str(r["criterion"]): r for r in rows}

    async def claim_calibration(self, owner: str, lease_s: float, max_attempts: int) -> Row | None:
        """A queued calibration, or one whose worker lost the lease and that
        has attempts left."""
        return await self.one(
            """UPDATE judge_calibration SET status = 'RUNNING', lease_owner = %s,
                   lease_expires_at = now() + make_interval(secs => %s), attempts = attempts + 1,
                   started_at = COALESCE(started_at, now()), updated_at = now()
               WHERE id = (SELECT id FROM judge_calibration
                           WHERE status = 'QUEUED'
                              OR (status = 'RUNNING' AND lease_expires_at < now() AND attempts < %s)
                           ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
               RETURNING *""",
            (owner, lease_s, max_attempts),
        )

    async def renew_calibration_lease(self, calibration_id: str, owner: str, lease_s: float) -> bool:
        row = await self.one(
            """UPDATE judge_calibration SET lease_expires_at = now() + make_interval(secs => %s),
                   updated_at = now()
               WHERE id = %s AND lease_owner = %s AND status = 'RUNNING' RETURNING id""",
            (lease_s, calibration_id, owner),
        )
        return row is not None

    async def fail_abandoned_calibrations(self, max_attempts: int) -> list[Row]:
        """Calibrations whose worker lost the lease on every attempt fail
        (the others are claimed again)."""
        return await self.all(
            """UPDATE judge_calibration SET status = 'FAILED', lease_owner = NULL, lease_expires_at = NULL,
                   error = 'Gave up after ' || attempts || ' attempts (the worker lost its lease each time).',
                   finished_at = now(), updated_at = now()
               WHERE status = 'RUNNING' AND lease_expires_at < now() AND attempts >= %s
               RETURNING id""",
            (max_attempts,),
        )

    async def finish_calibration(
        self, calibration_id: str, owner: str, values: Mapping[str, Any]
    ) -> Row | None:
        return await self.one(
            """UPDATE judge_calibration SET status = %s, judge = %s, metrics = %s, calibrated = %s,
                   reason = %s, disagreements = %s, error = %s, finished_at = now(), updated_at = now(),
                   lease_owner = NULL, lease_expires_at = NULL
               WHERE id = %s AND lease_owner = %s AND status = 'RUNNING' RETURNING *""",
            (
                values["status"],
                jsonb(values.get("judge")),
                jsonb(values.get("metrics")),
                values.get("calibrated"),
                values.get("reason"),
                jsonb(values.get("disagreements")),
                values.get("error"),
                calibration_id,
                owner,
            ),
        )


class JudgmentCache:
    """Judge verdicts in PostgreSQL, by everything that shaped them (ADR-0022)."""

    def __init__(self, store: Store) -> None:
        self.store = store

    async def get(self, key: str) -> JudgeVerdict | None:
        row = await self.store.get_judgment(key)
        return JudgeVerdict.from_json(row["verdict"]) if row is not None else None

    async def put(self, key: str, verdict: JudgeVerdict, judge: Mapping[str, str]) -> None:
        await self.store.put_judgment(key, verdict.to_json(), judge)
