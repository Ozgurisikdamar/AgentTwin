"""Human review (spec §16.5): a person's verdict on one expectation of one
side of a compared case replaces the evaluator's, and the case — and the run's
summary — are classified again from the stored snapshots.

POST /api/v1/eval-runs/{id}/cases/{scenario}/reviews   review an expectation
GET  /api/v1/reviews                                   the review queue

Reviews are append-only (the latest one for an expectation counts) and each is
announced to the audit log with the note as its reason. The queue lists the
cases of completed runs with an expectation the judge could not grade (ERROR,
SKIPPED) or graded, as a critical one, without being calibrated — until a
person reviewed it. What a review replaces, and how, is ``reviewing``.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from agenttwin.hashing import content_hash
from agenttwin_core import errors
from agenttwin_core.auth import Permission
from agenttwin_core.db import transaction
from agenttwin_core.ids import new_id
from agenttwin_core.logx import Log
from agenttwin_core.web import query_int, read_model, require
from agenttwin_evaluation.common import NAME, Strict, accessible, audit, check_uuid, pick, scope_of
from agenttwin_evaluation.comparison import CaseComparison, summarize
from agenttwin_evaluation.reviewing import SIDES, rebuild_case, review_json, reviewable
from agenttwin_evaluation.runs import eval_run_json
from agenttwin_evaluation.store import Row, Store, decode_cursor, encode_cursor

__all__ = ["ReviewsAPI"]


class ReviewBody(Strict):
    side: Literal["BASELINE", "CANDIDATE"]
    expectation_id: str = Field(min_length=1, max_length=100)
    status: Literal["PASS", "FAIL"]
    note: str = Field(min_length=1, max_length=2000)


class ReviewsAPI:
    def __init__(self, *, store: Store, log: Log) -> None:
        self.store = store
        self.log = log

    def routes(self, app: FastAPI) -> None:
        @app.post("/api/v1/eval-runs/{eval_run_id}/cases/{scenario}/reviews")
        async def review_expectation(eval_run_id: str, scenario: str, request: Request) -> JSONResponse:
            p = require(request, Permission.REVIEW_WRITE)
            check_uuid(eval_run_id, "eval_run_id")
            run = await self.store.get_eval_run(eval_run_id)
            if run is None or not accessible(p, run):
                raise errors.not_found()
            if not re.match(NAME, scenario):
                raise errors.invalid(
                    "INVALID_PARAMETER", "scenario is not a scenario name.", {"field": "scenario"}
                )
            body = await read_model(request, ReviewBody)
            async with transaction(self.store.pool) as conn:
                run = await self.store.lock_eval_run(conn, eval_run_id)
                assert run is not None  # noqa: S101 - runs are never deleted
                if run["status"] != "COMPLETED":
                    raise errors.conflict(
                        "EVAL_RUN_NOT_COMPLETED",
                        "Only the cases of a completed evaluation run can be reviewed.",
                    )
                rows = await self.store.case_rows_in(conn, eval_run_id)
                row = next((r for r in rows if r["scenario_name"] == scenario), None)
                if row is None:
                    raise errors.not_found("The evaluation run has no result for this scenario.")
                results = ((row["sides"] or {}).get(SIDES[body.side]) or {}).get("results") or []
                wanted = body.expectation_id
                current = next((r for r in results if str(r["expectation"]["id"]) == wanted), None)
                if current is None:
                    raise errors.not_found("This side of the case has no such expectation.")
                if not reviewable(current["expectation"]):
                    raise errors.conflict(
                        "EXPECTATION_NOT_REVIEWABLE",
                        "This is the simulation's finding about the run itself (the agent could not be run, "
                        "or did not finish), not an expectation a person can grade.",
                    )
                review = await self.store.insert_review(
                    conn,
                    {
                        "id": new_id(),
                        "eval_run_id": eval_run_id,
                        "organization_id": p.org_id,
                        "project_id": str(run["project_id"]),
                        "scenario_name": scenario,
                        "side": body.side,
                        "expectation_id": body.expectation_id,
                        "original_status": current["status"],
                        "original_label": current.get("label"),
                        "status": body.status,
                        "note": body.note,
                        "reviewer": p.actor,
                    },
                )
                reviews = await self.store.reviews_in(conn, eval_run_id)
                comparisons: list[CaseComparison] = []
                before = after = None
                for r in rows:
                    mine = [v for v in reviews if v["scenario_name"] == r["scenario_name"]]
                    comparison, sides = rebuild_case(r, mine)
                    comparisons.append(comparison)
                    if r["scenario_name"] == scenario:
                        before, after = r["classification"], comparison.classification
                        await self.store.update_case(
                            conn,
                            eval_run_id,
                            int(r["position"]),
                            {
                                "classification": after,
                                "comparison": comparison.to_json(),
                                "sides": sides,
                                "reviewed": True,
                            },
                        )
                done = await self.store.update_run_summary(conn, eval_run_id, summarize(comparisons))
                await audit(
                    conn,
                    p,
                    str(run["project_id"]),
                    "review.override",
                    "eval_case",
                    f"{eval_run_id}/{scenario}",
                    reason=body.note,
                    before_hash=content_hash({"status": current["status"], "label": current.get("label")}),
                    after_hash=content_hash({"status": body.status}),
                    metadata={
                        "side": body.side,
                        "expectation_id": body.expectation_id,
                        "from": current["status"],
                        "to": body.status,
                        "classification_before": before,
                        "classification_after": after,
                    },
                )
            assert done is not None  # noqa: S101 - the run is locked
            self.log.info(
                "expectation reviewed", eval_run_id=eval_run_id, scenario=scenario, status=body.status
            )
            return JSONResponse(
                {
                    "review": review_json(review),
                    "classification": after,
                    "previous_classification": before,
                    "run": eval_run_json(done),
                },
                status_code=201,
            )

        @app.get("/api/v1/reviews")
        async def review_queue(request: Request) -> dict[str, Any]:
            p = require(request, Permission.READ)
            q = request.query_params
            scope = scope_of(p, q.get("project_id") or None)
            try:
                after = decode_cursor(q.get("cursor") or "")
            except ValueError:
                raise errors.invalid("INVALID_CURSOR", "Cursor is malformed.") from None
            if after is not None and not re.fullmatch(r".+\|\d+", after.key):
                raise errors.invalid("INVALID_CURSOR", "Cursor is malformed.")
            limit = query_int(q, "limit", 50, 1, 200)
            rows = await self.store.review_queue(scope, after=after, limit=limit + 1)
            items = [_queue_item(r) for r in rows[:limit]]
            last = rows[limit - 1] if len(rows) > limit else None
            nxt = (
                encode_cursor(
                    f"{last['finished_at'].isoformat()}|{last['position']}", str(last["eval_run_id"])
                )
                if last is not None
                else None
            )
            return {"items": items, "next_cursor": nxt}


def _queue_item(row: Row) -> dict[str, Any]:
    done = set(row["done"] or ())
    pending = []
    for key in row["needs_review"]:
        if key in done:
            continue
        side, _, expectation_id = key.partition(":")
        results = ((row["sides"] or {}).get(SIDES.get(side, "")) or {}).get("results") or []
        result = next((r for r in results if str(r["expectation"]["id"]) == expectation_id), None)
        pending.append(
            {
                "side": side,
                "expectation_id": expectation_id,
                "status": result["status"] if result else None,
                "reason": result["reason"] if result else None,
                "critical": bool(result["critical"]) if result else False,
            }
        )
    out = pick(
        row,
        (
            "eval_run_id",
            "position",
            "scenario_name",
            "severity",
            "classification",
            "project_id",
            "agent_name",
            "baseline_version",
            "candidate_version",
            "finished_at",
        ),
    )
    return out | {"pending": pending}
