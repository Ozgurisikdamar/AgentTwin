"""Judge calibration (spec §16.4): the configured judge measured against
labels people gave, per criterion and project. The latest completed
calibration of a criterion decides whether the judge's verdicts on it count as
calibrated (recorded with every verdict; a release gate must not let an
uncalibrated judge alone block a release).

POST /api/v1/judges/calibrations        run a calibration (202, the worker runs it)
GET  /api/v1/judges/calibrations        list
GET  /api/v1/judges/calibrations/{id}   one, with its disagreements
GET  /api/v1/judges?project_id=         the judge and each criterion's calibration
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from agenttwin.hashing import content_hash
from agenttwin_core import errors
from agenttwin_core.auth import Permission, Principal
from agenttwin_core.db import transaction
from agenttwin_core.ids import new_id
from agenttwin_core.logx import Log
from agenttwin_core.web import query_int, read_model, require, require_project
from agenttwin_evaluation.calibration import MIN_AGREEMENT, MIN_EXAMPLES, MIN_KAPPA, LabeledExample, calibrate
from agenttwin_evaluation.common import Strict, accessible, audit, check_uuid, pick, scope_of
from agenttwin_evaluation.judges import CRITERIA, JudgeProvider, judge_identity
from agenttwin_evaluation.store import Row, Store, decode_cursor, encode_cursor

__all__ = ["CRITERION_NAMES", "JudgesAPI", "calibration_json", "run_calibration"]

CRITERION_NAMES = (*CRITERIA, "rubric")
Criterion = Literal["task_completion", "intent_fidelity", "relevance", "rubric"]


class ToolEvidence(Strict):
    ref: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=2000)


class ExampleBody(Strict):
    id: str = Field(min_length=1, max_length=100)
    rubric: str = Field(min_length=1, max_length=4000)
    customer_message: str = Field(max_length=8000)
    answer: str = Field(max_length=8000)
    tool_calls: list[ToolEvidence] = Field(default_factory=list, max_length=30)
    human_label: Literal["pass", "fail"]


class CalibrationBody(Strict):
    project_id: str
    criterion: str
    examples: list[ExampleBody] = Field(min_length=1, max_length=500)


def calibration_json(row: Mapping[str, Any], *, full: bool = False) -> dict[str, Any]:
    out = pick(
        row,
        (
            "id",
            "organization_id",
            "project_id",
            "criterion",
            "status",
            "example_count",
            "examples_sha256",
            "judge",
            "metrics",
            "calibrated",
            "reason",
            "error",
            "requested_by",
            "created_at",
            "started_at",
            "finished_at",
        ),
    )
    if full:
        out["disagreements"] = list(row.get("disagreements") or [])
    return out


async def run_calibration(store: Store, judge: JudgeProvider, row: Row, owner: str) -> Row | None:
    """Runs one claimed calibration to its end."""
    try:
        examples = [LabeledExample.from_json(e | {"criterion": row["criterion"]}) for e in row["examples"]]
        report = await calibrate(judge, judge_identity(judge), str(row["criterion"]), examples)
    except Exception as err:  # noqa: BLE001 - the calibration fails with its reason
        return await store.finish_calibration(
            str(row["id"]), owner, {"status": "FAILED", "error": f"{type(err).__name__}: {err}"}
        )
    return await store.finish_calibration(
        str(row["id"]),
        owner,
        {
            "status": "COMPLETED",
            "judge": report.judge,
            "metrics": report.metrics,
            "calibrated": report.calibrated,
            "reason": report.reason,
            "disagreements": list(report.disagreements),
        },
    )


class JudgesAPI:
    def __init__(self, *, store: Store, judge: JudgeProvider, log: Log) -> None:
        self.store = store
        self.judge = judge
        self.log = log

    async def _calibration_or_404(self, p: Principal, calibration_id: str) -> Row:
        check_uuid(calibration_id, "calibration_id")
        row = await self.store.get_calibration(calibration_id)
        if row is None or not accessible(p, row):
            raise errors.not_found()
        return row

    def routes(self, app: FastAPI) -> None:
        @app.post("/api/v1/judges/calibrations")
        async def start_calibration(request: Request) -> JSONResponse:
            body = await read_model(request, CalibrationBody)
            check_uuid(body.project_id, "project_id")
            p = require_project(request, Permission.REVIEW_WRITE, body.project_id)
            if body.criterion not in CRITERION_NAMES:
                raise errors.invalid(
                    "INVALID_PARAMETER",
                    f"criterion must be one of {', '.join(CRITERION_NAMES)}.",
                    {"field": "criterion"},
                )
            ids = [e.id for e in body.examples]
            if len(set(ids)) != len(ids):
                raise errors.invalid(
                    "INVALID_PARAMETER", "Example ids must be unique.", {"field": "examples"}
                )
            examples = [e.model_dump() for e in body.examples]
            async with transaction(self.store.pool) as conn:
                row = await self.store.insert_calibration(
                    conn,
                    {
                        "id": new_id(),
                        "organization_id": p.org_id,
                        "project_id": body.project_id,
                        "criterion": body.criterion,
                        "examples": examples,
                        "examples_sha256": content_hash(examples),
                        "requested_by": p.actor,
                    },
                )
                await audit(
                    conn,
                    p,
                    body.project_id,
                    "judge_calibration.start",
                    "judge_calibration",
                    str(row["id"]),
                    after_hash=str(row["examples_sha256"]),
                    metadata={"criterion": body.criterion, "examples": len(examples)},
                )
            return JSONResponse({"calibration": calibration_json(row, full=True)}, status_code=202)

        @app.get("/api/v1/judges/calibrations")
        async def list_calibrations(request: Request) -> dict[str, Any]:
            p = require(request, Permission.READ)
            q = request.query_params
            scope = scope_of(p, q.get("project_id") or None)
            criterion = q.get("criterion") or None
            if criterion is not None and criterion not in CRITERION_NAMES:
                raise errors.invalid("INVALID_PARAMETER", "Unknown criterion.", {"field": "criterion"})
            try:
                after = decode_cursor(q.get("cursor") or "")
            except ValueError:
                raise errors.invalid("INVALID_CURSOR", "Cursor is malformed.") from None
            limit = query_int(q, "limit", 50, 1, 200)
            rows = await self.store.list_calibrations(
                scope, criterion=criterion, after=after, limit=limit + 1
            )
            items = rows[:limit]
            nxt = encode_cursor(items[-1]["created_at"], str(items[-1]["id"])) if len(rows) > limit else None
            return {"items": [calibration_json(r) for r in items], "next_cursor": nxt}

        @app.get("/api/v1/judges/calibrations/{calibration_id}")
        async def get_calibration(calibration_id: str, request: Request) -> dict[str, Any]:
            p = require(request, Permission.READ)
            row = await self._calibration_or_404(p, calibration_id)
            return {"calibration": calibration_json(row, full=True)}

        @app.get("/api/v1/judges")
        async def describe_judge(request: Request) -> dict[str, Any]:
            p = require(request, Permission.READ)
            project = request.query_params.get("project_id") or ""
            if not project:
                raise errors.invalid("INVALID_PARAMETER", "project_id is required.", {"field": "project_id"})
            scope_of(p, project)
            identity = judge_identity(self.judge)
            latest = await self.store.latest_calibrations(p.org_id, project, identity)
            return {
                "judge": identity,
                "requirements": {
                    "min_examples": MIN_EXAMPLES,
                    "min_agreement": MIN_AGREEMENT,
                    "min_kappa": MIN_KAPPA,
                },
                "criteria": [
                    {
                        "criterion": name,
                        "calibrated": bool(latest[name]["calibrated"]) if name in latest else False,
                        "calibration": calibration_json(latest[name]) if name in latest else None,
                    }
                    for name in CRITERION_NAMES
                ],
            }
