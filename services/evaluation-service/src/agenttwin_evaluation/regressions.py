"""The regressions API (spec §18, §38, §41.3; ADR-0032): the inbox of
production failures the miner grouped, what people do with them, and the
promotion of one into a permanent regression test.

* ``GET  /api/v1/regressions/candidates``        the inbox (``read``)
* ``GET  /api/v1/regressions/{id}``              a group, its failures, its history
* ``GET  /api/v1/regressions/{id}/draft``        the scenario drafted from its representative trace
* ``PATCH /api/v1/regressions/{id}``             label, severity, tags, assignee (``review.write``)
* ``POST /api/v1/regressions/{id}/confirm|dismiss|reopen``   status (``review.write``)
* ``POST /api/v1/regressions/{id}/merge``        into another group of the agent (``review.write``)
* ``POST /api/v1/regressions/{id}/promote``      into a scenario (``regression.promote``)

Promotion writes in two steps (ADR-0032): the scenario is registered in the
simulation service (a repeat of the same document registers nothing new),
then one local transaction adds it to the project's
``production-regressions`` dataset, links the group and records the audit.
A promotion that fails after the first step is completed by its retry.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from agenttwin.hashing import content_hash
from agenttwin.redaction import Redactor
from agenttwin_core import errors
from agenttwin_core.auth import Permission, Principal
from agenttwin_core.db import Conn, transaction
from agenttwin_core.ids import new_id
from agenttwin_core.logx import Log
from agenttwin_core.schemas import document_validator
from agenttwin_core.web import query_int, read_model, require
from agenttwin_core.yamlsafe import YAMLDocumentError, dump_yaml, load_yaml
from agenttwin_evaluation.clients import ScenarioRefused, SimulationClient, TraceClient, UpstreamError
from agenttwin_evaluation.common import (
    MAX_CASES,
    TAG,
    Strict,
    accessible,
    audit,
    check_uuid,
    pick,
    scope_of,
    ts,
)
from agenttwin_evaluation.datasets import _cases_hash
from agenttwin_evaluation.drafting import Draft, NotAScenario, draft_scenario, prepare_promotion
from agenttwin_evaluation.mining import SEVERITIES, STATUSES, TAXONOMY, InvalidTransition, transition
from agenttwin_evaluation.regression_store import RegressionStore
from agenttwin_evaluation.store import Row, Store, decode_cursor, encode_cursor

__all__ = ["DATASET", "RegressionsAPI", "regression_json"]

#: The dataset every promoted regression joins.
DATASET = "production-regressions"
_ACTOR = re.compile(r"^(user|apikey|service):[A-Za-z0-9._@:-]{1,200}$")
_SCENARIO = document_validator("scenario.v1")
_REDACTOR = Redactor.all()


def _redact(text: str) -> str:
    return _REDACTOR.text(text)[0] or ""


def _s(value: Any) -> str | None:
    return None if value is None else str(value)


def regression_json(row: Row) -> dict[str, Any]:
    out = pick(
        row,
        (
            "title",
            "status",
            "taxonomy",
            "suggested_taxonomy",
            "severity",
            "suggested_severity",
            "severity_reason",
            "component",
            "assignee",
            "triaged_by",
            "representative_trace_id",
            "occurrence_count",
            "first_seen",
            "last_seen",
            "fingerprint",
            "scenario_name",
            "dataset_version",
            "promoted_by",
            "promoted_at",
            "fixed_version",
            "created_at",
            "updated_at",
        ),
    )
    return out | {
        "id": str(row["id"]),
        "organization_id": str(row["organization_id"]),
        "project_id": str(row["project_id"]),
        "agent": row["agent_name"],
        "secondary": list(row["secondary"] or []),
        "tags": list(row["tags"] or []),
        "evidence": list(row["evidence"] or []),
        "versions": list(row["versions"] or []),
        "environments": list(row["environments"] or []),
        "merged_into": _s(row["merged_into"]),
        "scenario_id": _s(row["scenario_id"]),
        "dataset_id": _s(row["dataset_id"]),
        "fixed_eval_run_id": _s(row["fixed_eval_run_id"]),
    }


def _occurrence_json(row: Row) -> dict[str, Any]:
    out = pick(
        row,
        (
            "trace_id",
            "agent_version",
            "environment",
            "started_at",
            "title",
            "component",
            "taxonomy",
            "severity",
            "severity_reason",
            "join_kind",
            "join_reason",
            "similarity",
            "created_at",
        ),
    )
    return out | {
        "secondary": list(row["secondary"] or []),
        "evidence": list(row["evidence"] or []),
        "reasons": list(row["reasons"] or []),
    }


def _event_json(row: Row) -> dict[str, Any]:
    return pick(row, ("seq", "action", "from_status", "to_status", "actor", "reason", "at")) | {
        "detail": dict(row["detail"] or {})
    }


def _csv(value: str | None, allowed: tuple[str, ...], field: str) -> list[str]:
    if not value:
        return []
    items = [v.strip() for v in value.split(",") if v.strip()]
    bad = [v for v in items if v not in allowed]
    if bad:
        raise errors.invalid("INVALID_PARAMETER", f"Unknown {field} {bad[0]!r}.", {"field": field})
    return items


def _reason(value: str | None, *, required: bool) -> str | None:
    text = (value or "").strip()
    if required and not text:
        raise errors.invalid("INVALID_REQUEST", "A reason is required.", {"field": "reason"})
    return text or None


class ReasonBody(Strict):
    reason: str | None = Field(default=None, max_length=2000)


class MergeBody(Strict):
    into: str
    reason: str | None = Field(default=None, max_length=2000)


class TriageBody(Strict):
    taxonomy: str | None = None
    severity: Literal["critical", "high", "medium", "low"] | None = None
    tags: list[str] | None = Field(default=None, max_length=20)
    assignee: str | None = Field(default=None, max_length=210)
    reason: str | None = Field(default=None, max_length=2000)


class PromoteBody(Strict):
    document: dict[str, Any] | None = None
    yaml: str | None = Field(default=None, max_length=600_000)
    reason: str | None = Field(default=None, max_length=2000)


class RegressionsAPI:
    def __init__(
        self,
        *,
        store: Store,
        regressions: RegressionStore,
        simulation: SimulationClient,
        traces: TraceClient,
        log: Log,
    ) -> None:
        self.store = store
        self.regressions = regressions
        self.simulation = simulation
        self.traces = traces
        self.log = log

    # ------------------------------------------------------------ helpers

    async def _visible(self, p: Principal, regression_id: str) -> Row:
        check_uuid(regression_id, "regression_id")
        row = await self.regressions.get(regression_id)
        if row is None or not accessible(p, row):
            raise errors.not_found()
        return row

    async def _locked(self, conn: Conn, row: Row) -> Row:
        """The group, locked after its agent's mining lock (the miner's
        order), and not merged into another."""
        await self.regressions.lock_agent(conn, str(row["project_id"]), str(row["agent_name"]))
        locked = await self.regressions.group(conn, str(row["id"]), lock=True)
        assert locked is not None  # noqa: S101 - groups people acted on are never deleted
        if locked["merged_into"] is not None:
            raise errors.conflict(
                "REGRESSION_MERGED",
                "This regression was merged into another one; act on that one.",
                {"merged_into": str(locked["merged_into"])},
            )
        return locked

    @staticmethod
    def _next(row: Row, action: Any) -> str:
        try:
            return transition(str(row["status"]), action, has_case=row["scenario_name"] is not None)
        except InvalidTransition as err:
            raise errors.conflict(
                "REGRESSION_TRANSITION_INVALID", str(err), {"status": row["status"], "action": action}
            ) from None

    async def _twin(self, org: str, project: str, agent: str) -> dict[str, Any] | None:
        """The twin the project's scenarios of this agent use most (any
        scenario's, if none of the agent's names one)."""
        scenarios = await self.simulation.scenarios(org, project)
        counts = Counter(str(s["twin"]) for s in scenarios if s.get("agent") == agent and s.get("twin"))
        if not counts:
            counts = Counter(str(s["twin"]) for s in scenarios if s.get("twin"))
        if not counts:
            return None
        name = min(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        return await self.simulation.twin_document(org, project, name)

    async def _draft(self, row: Row) -> Draft:
        org, project = str(row["organization_id"]), str(row["project_id"])
        trace_id = str(row["representative_trace_id"])
        try:
            detail = await self.traces.trace(org, project, trace_id)
            if detail is None:
                raise errors.conflict(
                    "REGRESSION_TRACE_GONE",
                    "The trace that represents this regression is no longer kept; write the scenario "
                    "and promote it with the document.",
                    {"trace_id": trace_id},
                )
            twin = await self._twin(org, project, str(row["agent_name"]))
        except UpstreamError as err:
            raise errors.unavailable(f"The draft needs a service that did not answer: {err}") from None
        group = dict(row) | {"id": str(row["id"])}
        return draft_scenario(trace=detail, twin=twin, group=group, redact=_redact)

    async def _join_dataset(
        self, conn: Conn, p: Principal, org: str, project: str, case: dict[str, Any]
    ) -> tuple[str, int]:
        """Adds (or refreshes) the case in the project's regression dataset,
        creating it with the first one; returns the dataset and its version."""
        name = str(case["scenario"])
        dataset = await self.store.dataset_named(conn, project, DATASET)
        if dataset is None:
            dataset_id = new_id()
            created = await self.store.insert_dataset(
                conn,
                {
                    "id": dataset_id,
                    "organization_id": org,
                    "project_id": project,
                    "name": DATASET,
                    "description": "Production failures promoted to regression tests (ADR-0032).",
                    "tags": ["production-regression"],
                    "created_by": p.actor,
                },
                {"id": new_id(), "cases": [case], "note": f"promoted {name}", "created_by": p.actor},
            )
            if created is not None:
                await audit(
                    conn,
                    p,
                    project,
                    "dataset.create",
                    "dataset",
                    dataset_id,
                    after_hash=_cases_hash([case]),
                    metadata={"name": DATASET, "version": 1, "cases": 1},
                )
                return dataset_id, 1
            # Another promotion created it first (its insert committed while
            # this one waited on the name): read and extend it.
            dataset = await self.store.dataset_named(conn, project, DATASET)
            assert dataset is not None  # noqa: S101 - the conflicting row committed
        dataset_id = str(dataset["id"])
        if dataset["archived"]:
            raise errors.conflict(
                "REGRESSION_DATASET_ARCHIVED",
                f"The {DATASET} dataset is archived; promoted regressions cannot join it.",
            )
        latest = await self.store.version_in(conn, dataset_id, int(dataset["latest_version"]))
        assert latest is not None  # noqa: S101 - the latest version exists
        cases = list(latest["cases"])
        at = next((i for i, c in enumerate(cases) if c.get("scenario") == name), None)
        # A scenario promoted again (a person reused its name) keeps one case: the latest.
        updated = [*cases, case] if at is None else [*cases[:at], case, *cases[at + 1 :]]
        if len(updated) > MAX_CASES:
            raise errors.conflict("TOO_MANY_CASES", f"The {DATASET} dataset holds at most {MAX_CASES} cases.")
        bumped = await self.store.add_dataset_version(
            conn,
            dataset_id,
            {"id": new_id(), "cases": updated, "note": f"promoted {name}", "created_by": p.actor},
        )
        version = int(bumped["latest_version"])
        await audit(
            conn,
            p,
            project,
            "dataset.update",
            "dataset",
            dataset_id,
            reason=f"promoted {name}",
            before_hash=_cases_hash(cases),
            after_hash=_cases_hash(updated),
            metadata={"name": DATASET, "version": version, "cases": len(updated)},
        )
        return dataset_id, version

    # ------------------------------------------------------------ routes

    def routes(self, app: FastAPI) -> None:
        @app.get("/api/v1/regressions/candidates")
        async def list_regressions(request: Request) -> dict[str, Any]:
            p = require(request, Permission.READ)
            q = request.query_params
            scope = scope_of(p, q.get("project_id") or None)
            try:
                after = decode_cursor(q.get("cursor") or "")
            except ValueError:
                raise errors.invalid("INVALID_CURSOR", "Cursor is malformed.") from None
            taxonomy = q.get("taxonomy") or None
            if taxonomy is not None and taxonomy not in TAXONOMY:
                raise errors.invalid(
                    "INVALID_PARAMETER", f"Unknown taxonomy {taxonomy!r}.", {"field": "taxonomy"}
                )
            merged = q.get("include_merged", "false")
            if merged not in ("true", "false"):
                raise errors.invalid(
                    "INVALID_PARAMETER", "include_merged is true or false.", {"field": "include_merged"}
                )
            agent = q.get("agent") or None
            if agent is not None and len(agent) > 200:
                raise errors.invalid("INVALID_PARAMETER", "agent is too long.", {"field": "agent"})
            limit = query_int(q, "limit", 50, 1, 200)
            rows = await self.regressions.list_groups(
                scope,
                statuses=_csv(q.get("status"), STATUSES, "status"),
                severities=_csv(q.get("severity"), SEVERITIES, "severity"),
                taxonomy=taxonomy,
                agent=agent,
                include_merged=merged == "true",
                after=after,
                limit=limit + 1,
            )
            items = [regression_json(r) for r in rows[:limit]]
            last = rows[limit - 1] if len(rows) > limit else None
            nxt = encode_cursor(last["last_seen"], str(last["id"])) if last is not None else None
            return {"items": items, "next_cursor": nxt}

        @app.get("/api/v1/regressions/{regression_id}")
        async def get_regression(regression_id: str, request: Request) -> dict[str, Any]:
            p = require(request, Permission.READ)
            row = await self._visible(p, regression_id)
            occurrences = await self.regressions.occurrences(regression_id)
            events = await self.regressions.events(regression_id)
            return {
                "regression": regression_json(row),
                "occurrences": [_occurrence_json(o) for o in occurrences],
                "events": [_event_json(e) for e in events],
            }

        @app.get("/api/v1/regressions/{regression_id}/draft")
        async def draft_regression(regression_id: str, request: Request) -> dict[str, Any]:
            p = require(request, Permission.READ)
            row = await self._visible(p, regression_id)
            draft = await self._draft(row)
            return {
                "regression_id": regression_id,
                "trace_id": str(row["representative_trace_id"]),
                "draft": draft.to_json() | {"yaml": dump_yaml(draft.document)},
            }

        @app.patch("/api/v1/regressions/{regression_id}")
        async def triage_regression(regression_id: str, request: Request) -> dict[str, Any]:
            p = require(request, Permission.REVIEW_WRITE)
            row = await self._visible(p, regression_id)
            body = await read_model(request, TriageBody)
            given = body.model_fields_set - {"reason"}
            if not given:
                raise errors.invalid("INVALID_REQUEST", "Nothing to change.")
            if "taxonomy" in given and body.taxonomy not in TAXONOMY:
                raise errors.invalid("INVALID_PARAMETER", "Unknown taxonomy.", {"field": "taxonomy"})
            if "severity" in given and body.severity is None:
                raise errors.invalid("INVALID_PARAMETER", "severity cannot be empty.", {"field": "severity"})
            tags: list[str] | None = None
            if "tags" in given:
                tags = sorted(set(body.tags or []))
                bad = next((t for t in tags if not TAG.match(t)), None)
                if bad is not None:
                    raise errors.invalid("INVALID_PARAMETER", f"Invalid tag {bad!r}.", {"field": "tags"})
            if "assignee" in given and body.assignee is not None and not _ACTOR.match(body.assignee):
                raise errors.invalid(
                    "INVALID_PARAMETER",
                    "assignee is who is assigned: user:<id>, apikey:<id> or service:<name>.",
                    {"field": "assignee"},
                )
            reason = _reason(body.reason, required=False)
            async with transaction(self.regressions.pool) as conn:
                locked = await self._locked(conn, row)
                changes: dict[str, list[Any]] = {}
                if "taxonomy" in given and body.taxonomy != locked["taxonomy"]:
                    changes["taxonomy"] = [locked["taxonomy"], body.taxonomy]
                if "severity" in given and body.severity != locked["severity"]:
                    changes["severity"] = [locked["severity"], body.severity]
                if tags is not None and tags != list(locked["tags"] or []):
                    changes["tags"] = [list(locked["tags"] or []), tags]
                if "assignee" in given and body.assignee != locked["assignee"]:
                    changes["assignee"] = [locked["assignee"], body.assignee]
                if not changes:
                    return {"regression": regression_json(locked)}
                values: dict[str, Any] = {k: v[1] for k, v in changes.items()}
                if "taxonomy" in changes or "severity" in changes:
                    values["triaged_by"] = p.actor
                    if "severity" in changes:
                        values["severity_reason"] = f"set by {p.actor}"
                done = await self.regressions.update_group(conn, regression_id, values)
                triage = {k: v for k, v in changes.items() if k != "assignee"}
                if triage:
                    await self.regressions.add_event(
                        conn, regression_id, action="triage", actor=p.actor, reason=reason, detail=triage
                    )
                if "assignee" in changes:
                    await self.regressions.add_event(
                        conn,
                        regression_id,
                        action="assign",
                        actor=p.actor,
                        reason=reason,
                        detail={"assignee": changes["assignee"]},
                    )
                await audit(
                    conn,
                    p,
                    str(row["project_id"]),
                    "regression.triage",
                    "regression",
                    regression_id,
                    reason=reason,
                    before_hash=content_hash({k: v[0] for k, v in changes.items()}),
                    after_hash=content_hash({k: v[1] for k, v in changes.items()}),
                    metadata={"changes": changes},
                )
            self.log.info("regression triaged", regression_group_id=regression_id, fields=sorted(changes))
            return {"regression": regression_json(done)}

        async def change_status(
            regression_id: str, request: Request, action: Literal["confirm", "dismiss", "reopen"]
        ) -> dict[str, Any]:
            p = require(request, Permission.REVIEW_WRITE)
            row = await self._visible(p, regression_id)
            body = await read_model(request, ReasonBody)
            reason = _reason(body.reason, required=action != "confirm")
            async with transaction(self.regressions.pool) as conn:
                locked = await self._locked(conn, row)
                to = self._next(locked, action)
                done = await self.regressions.set_status(conn, regression_id, to)
                await self.regressions.add_event(
                    conn,
                    regression_id,
                    action=action,
                    actor=p.actor,
                    from_status=str(locked["status"]),
                    to_status=to,
                    reason=reason,
                )
                await audit(
                    conn,
                    p,
                    str(row["project_id"]),
                    f"regression.{action}",
                    "regression",
                    regression_id,
                    reason=reason,
                    metadata={"from": locked["status"], "to": to},
                )
            self.log.info("regression status changed", regression_group_id=regression_id, status=to)
            return {"regression": regression_json(done)}

        @app.post("/api/v1/regressions/{regression_id}/confirm")
        async def confirm_regression(regression_id: str, request: Request) -> dict[str, Any]:
            return await change_status(regression_id, request, "confirm")

        @app.post("/api/v1/regressions/{regression_id}/dismiss")
        async def dismiss_regression(regression_id: str, request: Request) -> dict[str, Any]:
            return await change_status(regression_id, request, "dismiss")

        @app.post("/api/v1/regressions/{regression_id}/reopen")
        async def reopen_regression(regression_id: str, request: Request) -> dict[str, Any]:
            return await change_status(regression_id, request, "reopen")

        @app.post("/api/v1/regressions/{regression_id}/merge")
        async def merge_regression(regression_id: str, request: Request) -> dict[str, Any]:
            p = require(request, Permission.REVIEW_WRITE)
            row = await self._visible(p, regression_id)
            body = await read_model(request, MergeBody)
            check_uuid(body.into, "into")
            if body.into == regression_id:
                raise errors.invalid(
                    "INVALID_REQUEST", "A regression cannot be merged into itself.", {"field": "into"}
                )
            target_row = await self.regressions.get(body.into)
            if (
                target_row is None
                or not accessible(p, target_row)
                or target_row["project_id"] != row["project_id"]
            ):
                raise errors.not_found("There is no such regression in this project to merge into.")
            reason = _reason(body.reason, required=False)
            async with transaction(self.regressions.pool) as conn:
                await self.regressions.lock_agent(conn, str(row["project_id"]), str(row["agent_name"]))
                first, second = sorted([regression_id, body.into])
                locks = {
                    first: await self.regressions.group(conn, first, lock=True),
                    second: await self.regressions.group(conn, second, lock=True),
                }
                source, target = locks[regression_id], locks[body.into]
                assert source is not None and target is not None  # noqa: S101 - checked above, never deleted
                for g in (source, target):
                    if g["merged_into"] is not None:
                        raise errors.conflict(
                            "REGRESSION_MERGED",
                            "A regression that was merged into another one cannot be merged again.",
                            {"regression_id": str(g["id"]), "merged_into": str(g["merged_into"])},
                        )
                if source["agent_name"] != target["agent_name"]:
                    raise errors.conflict(
                        "REGRESSION_AGENT_MISMATCH", "Only regressions of the same agent can be merged."
                    )
                if source["scenario_name"] is not None:
                    raise errors.conflict(
                        "REGRESSION_HAS_TEST",
                        "This regression became a test; merge the others into it instead.",
                        {"scenario": source["scenario_name"]},
                    )
                moved = int(source["occurrence_count"])
                await self.regressions.merge(conn, regression_id, body.into)
                merged = await self.regressions.refresh_group(conn, regression_id)
                done = await self.regressions.refresh_group(conn, body.into)
                assert merged is not None and done is not None  # noqa: S101 - locked
                await self.regressions.add_event(
                    conn,
                    regression_id,
                    action="merged",
                    actor=p.actor,
                    reason=reason,
                    detail={"into": body.into, "occurrences": moved},
                )
                await self.regressions.add_event(
                    conn,
                    body.into,
                    action="merge",
                    actor=p.actor,
                    reason=reason,
                    detail={"from": regression_id, "occurrences": moved},
                )
                await audit(
                    conn,
                    p,
                    str(row["project_id"]),
                    "regression.merge",
                    "regression",
                    regression_id,
                    reason=reason,
                    metadata={"into": body.into, "occurrences": moved},
                )
            self.log.info("regression merged", regression_group_id=regression_id, into=body.into)
            return {"regression": regression_json(done), "merged": regression_json(merged)}

        @app.post("/api/v1/regressions/{regression_id}/promote")
        async def promote_regression(regression_id: str, request: Request) -> JSONResponse:
            p = require(request, Permission.REGRESSION_PROMOTE)
            row = await self._visible(p, regression_id)
            body = await read_model(request, PromoteBody)
            if body.document is not None and body.yaml is not None:
                raise errors.invalid("INVALID_REQUEST", "Give the document or its YAML, not both.")
            reason = _reason(body.reason, required=False)
            if row["merged_into"] is not None:
                raise errors.conflict(
                    "REGRESSION_MERGED",
                    "This regression was merged into another one; act on that one.",
                    {"merged_into": str(row["merged_into"])},
                )
            self._next(row, "promote")
            document: Any = body.document
            if body.yaml is not None:
                try:
                    document = load_yaml(body.yaml)
                except YAMLDocumentError as err:
                    raise errors.invalid("SCENARIO_INVALID", f"The YAML does not parse: {err}") from None
            if document is None:
                draft = await self._draft(row)
                if not draft.complete:
                    raise errors.conflict(
                        "REGRESSION_DRAFT_INCOMPLETE",
                        "The draft is missing what only a person can write; "
                        "edit it, then promote the document.",
                        {"problems": draft.problems},
                    )
                document = draft.document
            trace_id = str(row["representative_trace_id"])
            try:
                document, redacted = prepare_promotion(document, group=row, trace_id=trace_id, redact=_redact)
            except NotAScenario as err:
                raise errors.invalid("SCENARIO_INVALID", str(err)) from None
            spec = document["spec"]
            agent = str(row["agent_name"])
            if spec.get("agent") not in (None, agent):
                raise errors.invalid(
                    "SCENARIO_INVALID",
                    f"The scenario is for {spec.get('agent')!r}, the regression for {agent!r}.",
                )
            spec["agent"] = agent
            problems = sorted(
                f"{'/'.join(str(x) for x in e.absolute_path) or '(document)'}: {e.message}"
                for e in _SCENARIO.iter_errors(document)
            )
            if problems:
                raise errors.invalid(
                    "SCENARIO_INVALID", "The scenario is not valid.", {"errors": problems[:20]}
                )
            org, project = str(row["organization_id"]), str(row["project_id"])
            try:
                registered = await self.simulation.create_scenario(org, project, document)
            except ScenarioRefused as err:
                raise errors.invalid(
                    "SCENARIO_INVALID", str(err), {"code": err.code, **err.details}
                ) from None
            except UpstreamError as err:
                raise errors.unavailable(
                    f"The simulation service did not register the scenario: {err}"
                ) from None
            scenario = registered["scenario"]
            name = str(scenario["name"])
            now = ts(datetime.now(UTC))
            case = {
                "scenario": name,
                "tags": sorted({t for t in document["metadata"].get("tags") or [] if TAG.match(str(t))})[:20],
                "source": "production_regression",
                "trace_id": trace_id,
                "privacy": "redacted",
                "note": f"Promoted from the regression {str(row['title'])[:1900]!r}.",
                "added_by": p.actor,
                "added_at": now,
            }
            async with transaction(self.regressions.pool) as conn:
                locked = await self._locked(conn, row)
                to = self._next(locked, "promote")
                dataset_id, version = await self._join_dataset(conn, p, org, project, case)
                done = await self.regressions.set_status(
                    conn,
                    regression_id,
                    to,
                    {
                        "scenario_id": str(scenario["id"]),
                        "scenario_name": name,
                        "dataset_id": dataset_id,
                        "dataset_version": version,
                        "promoted_by": p.actor,
                        "promoted_at": datetime.now(UTC),
                    },
                )
                detail = {
                    "scenario": name,
                    "scenario_version": registered.get("version"),
                    "dataset": DATASET,
                    "dataset_version": version,
                    "redacted": redacted,
                }
                await self.regressions.add_event(
                    conn,
                    regression_id,
                    action="promote",
                    actor=p.actor,
                    from_status=str(locked["status"]),
                    to_status=to,
                    reason=reason,
                    detail=detail,
                )
                await audit(
                    conn,
                    p,
                    project,
                    "regression.promote",
                    "regression",
                    regression_id,
                    reason=reason,
                    after_hash=content_hash(document),
                    metadata=detail | {"from": locked["status"], "to": to, "trace_id": trace_id},
                )
            self.log.info(
                "regression promoted",
                regression_group_id=regression_id,
                scenario=name,
                dataset_version=version,
            )
            return JSONResponse(
                {
                    "regression": regression_json(done),
                    "scenario": {
                        "id": str(scenario["id"]),
                        "name": name,
                        "version": registered.get("version"),
                        "created": bool(registered.get("created")),
                        "warnings": [str(w) for w in registered.get("warnings") or []],
                    },
                    "dataset": {"id": dataset_id, "name": DATASET, "version": version},
                    "redacted": redacted,
                },
                status_code=201,
            )
