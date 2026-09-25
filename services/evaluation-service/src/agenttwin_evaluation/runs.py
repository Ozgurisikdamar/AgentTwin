"""Evaluation runs (spec §27): a candidate agent version against its baseline
on one pinned suite, compared case by case.

POST /api/v1/eval-runs                          request (202, QUEUED)
GET  /api/v1/eval-runs                          list
GET  /api/v1/eval-runs/{id}                     the run, its summary, its cases, its transitions
GET  /api/v1/eval-runs/{id}/cases/{scenario}    one compared case in full
POST /api/v1/eval-runs/{id}/cancel              request cancellation

The suite is a dataset version (its scenario names, pinned when the run is
requested), a list of scenario names and/or tags, or every scenario of the
agent. The worker (``agenttwin_evaluation.worker``) does the rest.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import Field, model_validator

from agenttwin_core import errors
from agenttwin_core.auth import Permission, Principal
from agenttwin_core.db import transaction
from agenttwin_core.events import write_outbox
from agenttwin_core.ids import new_id
from agenttwin_core.jobs import JobStatus
from agenttwin_core.logx import Log
from agenttwin_core.web import query_int, read_model, require, require_project
from agenttwin_evaluation.common import NAME, TAG, Strict, accessible, audit, check_uuid, pick, scope_of, ts
from agenttwin_evaluation.reviewing import review_json
from agenttwin_evaluation.store import SCHEMA, Row, Store, decode_cursor, encode_cursor
from agenttwin_evaluation.worker import completed_event

__all__ = ["EvalRunsAPI", "eval_run_json"]

AGENT = r"^[a-z0-9][a-z0-9_-]{0,62}$"
STATUSES = tuple(str(s) for s in JobStatus)


class StartEvalRunBody(Strict):
    project_id: str
    agent: str = Field(pattern=AGENT)
    baseline_version: str = Field(min_length=1, max_length=100)
    candidate_version: str = Field(min_length=1, max_length=100)
    dataset_id: str | None = None
    dataset_version: int | None = Field(default=None, ge=1)
    scenarios: list[str] | None = Field(default=None, max_length=500)
    tags: list[str] | None = Field(default=None, max_length=50)
    seed: int | None = Field(default=None, ge=0, le=2**32 - 1)
    release_id: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _one_suite(self) -> StartEvalRunBody:
        if self.dataset_id is not None and (self.scenarios is not None or self.tags is not None):
            raise ValueError("Give a dataset or scenarios and tags, not both.")
        if self.dataset_version is not None and self.dataset_id is None:
            raise ValueError("dataset_version needs dataset_id.")
        for name in self.scenarios or ():
            if not re.match(NAME, name):
                raise ValueError(f"{name!r} is not a scenario name.")
        for tag in self.tags or ():
            if not TAG.match(tag):
                raise ValueError(f"{tag!r} is not a tag.")
        return self


_RUN_KEYS = (
    "id",
    "organization_id",
    "project_id",
    "agent_name",
    "baseline_version",
    "candidate_version",
    "seed",
    "release_id",
    "status",
    "requested_by",
    "cancel_requested",
    "attempts",
    "baseline_run_id",
    "candidate_run_id",
    "case_count",
    "error",
    "created_at",
    "started_at",
    "finished_at",
    "updated_at",
)


def eval_run_json(row: Mapping[str, Any]) -> dict[str, Any]:
    out = pick(row, _RUN_KEYS)
    selection = row.get("selection") or {}
    out["selection"] = {
        "scenarios": selection.get("scenarios"),
        "tags": selection.get("tags"),
        "dataset": selection.get("dataset"),
    }
    out["counts"] = {
        "NEW_CRITICAL_FAILURE": int(row.get("new_critical_failures") or 0),
        "REGRESSED": int(row.get("regressed") or 0),
        "IMPROVED": int(row.get("improved") or 0),
        "UNCHANGED": int(row.get("unchanged") or 0),
        "INCOMPLETE": int(row.get("incomplete") or 0),
    }
    pinning = row.get("pinning") or {}
    out["pinning"] = (
        {k: pinning.get(k) for k in ("seed", "baseline", "candidate")} | {"cases": pinning.get("cases") or []}
        if pinning
        else None
    )
    out["judge"] = row.get("judge")
    out["budget"] = row.get("budget")
    return out


def _case_summary_json(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "position": int(row["position"]),
        "scenario_name": row["scenario_name"],
        "severity": row["severity"],
        "tags": list(row.get("tags") or []),
        "classification": row["classification"],
        "reason": row["reason"],
        "baseline": row["baseline"],
        "candidate": row["candidate"],
        "reviewed": bool(row["reviewed"]),
    }


class EvalRunsAPI:
    def __init__(self, *, store: Store, log: Log) -> None:
        self.store = store
        self.log = log

    async def _run_or_404(self, p: Principal, run_id: str) -> Row:
        check_uuid(run_id, "eval_run_id")
        row = await self.store.get_eval_run(run_id)
        if row is None or not accessible(p, row):
            raise errors.not_found()
        return row

    async def _suite(self, p: Principal, body: StartEvalRunBody) -> dict[str, Any]:
        if body.dataset_id is None:
            return {
                "scenarios": sorted(set(body.scenarios)) if body.scenarios is not None else None,
                "tags": sorted(set(body.tags)) if body.tags is not None else None,
                "dataset": None,
            }
        check_uuid(body.dataset_id, "dataset_id")
        dataset = await self.store.get_dataset(body.dataset_id)
        if dataset is None or not accessible(p, dataset) or str(dataset["project_id"]) != body.project_id:
            raise errors.not_found("No such dataset in this project.")
        if dataset["archived"]:
            raise errors.conflict(
                "DATASET_ARCHIVED", "The dataset is archived; it can no longer be evaluated."
            )
        version = body.dataset_version or int(dataset["latest_version"])
        v = await self.store.dataset_version(body.dataset_id, version)
        if v is None:
            raise errors.not_found("The dataset has no such version.")
        names = [str(c["scenario"]) for c in v["cases"]]
        if not names:
            raise errors.invalid("DATASET_EMPTY", "The dataset version has no cases to evaluate.")
        return {
            "scenarios": sorted(names),
            "tags": None,
            "dataset": {"id": body.dataset_id, "name": dataset["name"], "version": version},
        }

    def routes(self, app: FastAPI) -> None:
        @app.post("/api/v1/eval-runs")
        async def start_eval_run(request: Request) -> JSONResponse:
            body = await read_model(request, StartEvalRunBody)
            check_uuid(body.project_id, "project_id")
            p = require_project(request, Permission.EVAL_RUN, body.project_id)
            selection = await self._suite(p, body)
            dataset = selection["dataset"]
            run_id = new_id()
            async with transaction(self.store.pool) as conn:
                row = await self.store.insert_eval_run(
                    conn,
                    {
                        "id": run_id,
                        "organization_id": p.org_id,
                        "project_id": body.project_id,
                        "agent_name": body.agent,
                        "baseline_version": body.baseline_version,
                        "candidate_version": body.candidate_version,
                        "dataset_id": dataset["id"] if dataset else None,
                        "dataset_version": dataset["version"] if dataset else None,
                        "selection": selection,
                        "seed": body.seed,
                        "release_id": body.release_id,
                        "requested_by": p.actor,
                    },
                )
                await audit(
                    conn,
                    p,
                    body.project_id,
                    "eval_run.start",
                    "eval_run",
                    run_id,
                    metadata={
                        "agent": body.agent,
                        "baseline_version": body.baseline_version,
                        "candidate_version": body.candidate_version,
                        "dataset": dataset,
                    },
                )
            self.log.info("eval run requested", eval_run_id=run_id, agent=body.agent)
            return JSONResponse({"run": eval_run_json(row)}, status_code=202)

        @app.get("/api/v1/eval-runs")
        async def list_eval_runs(request: Request) -> dict[str, Any]:
            p = require(request, Permission.READ)
            q = request.query_params
            scope = scope_of(p, q.get("project_id") or None)
            status = q.get("status") or None
            if status is not None and status not in STATUSES:
                raise errors.invalid("INVALID_PARAMETER", "Unknown status.", {"field": "status"})
            agent = q.get("agent") or None
            if agent is not None and not re.match(AGENT, agent):
                raise errors.invalid("INVALID_PARAMETER", "agent is not an agent name.", {"field": "agent"})
            dataset_id = q.get("dataset_id") or None
            check_uuid(dataset_id, "dataset_id")
            try:
                after = decode_cursor(q.get("cursor") or "")
            except ValueError:
                raise errors.invalid("INVALID_CURSOR", "Cursor is malformed.") from None
            limit = query_int(q, "limit", 50, 1, 200)
            rows = await self.store.list_eval_runs(
                scope, status=status, agent=agent, dataset_id=dataset_id, after=after, limit=limit + 1
            )
            items = rows[:limit]
            nxt = encode_cursor(items[-1]["created_at"], str(items[-1]["id"])) if len(rows) > limit else None
            return {"items": [eval_run_json(r) for r in items], "next_cursor": nxt}

        @app.get("/api/v1/eval-runs/{eval_run_id}")
        async def get_eval_run(eval_run_id: str, request: Request) -> dict[str, Any]:
            p = require(request, Permission.READ)
            row = await self._run_or_404(p, eval_run_id)
            return {
                "run": eval_run_json(row),
                "summary": row["summary"],
                "cases": [_case_summary_json(c) for c in await self.store.case_results(eval_run_id)],
                "transitions": [
                    pick(t, ("from_status", "to_status", "reason", "at"))
                    for t in await self.store.eval_run_transitions(eval_run_id)
                ],
            }

        @app.get("/api/v1/eval-runs/{eval_run_id}/cases/{scenario}")
        async def get_eval_case(eval_run_id: str, scenario: str, request: Request) -> dict[str, Any]:
            p = require(request, Permission.READ)
            await self._run_or_404(p, eval_run_id)
            if not re.match(NAME, scenario):
                raise errors.invalid(
                    "INVALID_PARAMETER", "scenario is not a scenario name.", {"field": "scenario"}
                )
            row = await self.store.case_result(eval_run_id, scenario)
            if row is None:
                raise errors.not_found("The evaluation run has no result for this scenario.")
            sides = row["sides"] or {}
            return {
                "eval_run_id": eval_run_id,
                "position": int(row["position"]),
                "reviewed": bool(row["reviewed"]),
                "comparison": row["comparison"],
                "results": {
                    side: (sides.get(side) or {}).get("results") or [] for side in ("baseline", "candidate")
                },
                "verdicts": {
                    side: (sides.get(side) or {}).get("verdicts") or [] for side in ("baseline", "candidate")
                },
                "needs_review": list(row["needs_review"] or []),
                "reviews": [review_json(v) for v in await self.store.reviews_of(eval_run_id, scenario)],
                "updated_at": ts(row["updated_at"]),
            }

        @app.post("/api/v1/eval-runs/{eval_run_id}/cancel")
        async def cancel_eval_run(eval_run_id: str, request: Request) -> JSONResponse:
            p = require(request, Permission.EVAL_RUN)
            row = await self._run_or_404(p, eval_run_id)
            if JobStatus(row["status"]).terminal:
                return JSONResponse({"run": eval_run_json(row)}, status_code=200)
            async with transaction(self.store.pool) as conn:
                marked = await self.store.request_cancel(conn, eval_run_id)
                assert marked is not None  # noqa: S101 - the run exists
                done = None
                if marked["status"] == "QUEUED":
                    done = await self.store.transition(
                        conn, eval_run_id, JobStatus.CANCELLED, "cancelled while queued"
                    )
                    if done is not None:
                        await write_outbox(conn, SCHEMA, completed_event(done, JobStatus.CANCELLED, None))
                if not row["cancel_requested"]:
                    await audit(conn, p, str(row["project_id"]), "eval_run.cancel", "eval_run", eval_run_id)
            final = done or marked
            return JSONResponse({"run": eval_run_json(final)}, status_code=200 if done is not None else 202)
