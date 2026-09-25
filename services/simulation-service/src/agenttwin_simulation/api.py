"""The simulation service's public API (reached through the control plane,
which authenticates the caller and forwards an internal token).

Twins      POST /api/v1/twins · GET /api/v1/twins · GET /api/v1/twins/{id}
Scenarios  POST /api/v1/scenarios/validate · POST /api/v1/scenarios
           GET /api/v1/scenarios · GET /api/v1/scenarios/{id}
           POST /api/v1/scenarios/{id}/archive
Runs       GET /api/v1/simulations/capabilities
           POST /api/v1/simulations · GET /api/v1/simulations
           GET /api/v1/simulations/{id} · GET /api/v1/simulations/{id}/cases/{case_id}
           POST /api/v1/simulations/{id}/cancel

Resources of projects outside the caller's scope answer 404 (no probing).
"""

from __future__ import annotations

import base64
import json
import re
import secrets
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from agenttwin_core import errors
from agenttwin_core.auth import Permission, Principal
from agenttwin_core.db import Conn, transaction
from agenttwin_core.errors import APIError
from agenttwin_core.evaluators import Registry
from agenttwin_core.events import Envelope, write_outbox
from agenttwin_core.ids import new_id, valid_uuid
from agenttwin_core.jobs import JobStatus
from agenttwin_core.logx import Log, request_id_var
from agenttwin_core.web import query_int, read_model, require, require_project
from agenttwin_simulation.cases import case_seed, case_tenant
from agenttwin_simulation.clients import ControlPlaneClient, UpstreamError
from agenttwin_simulation.config import SimulationConfig
from agenttwin_simulation.documents import (
    DocumentProblem,
    ScenarioCheck,
    check_scenario,
    export_yaml,
    parse_document,
    scenario_twin_problems,
    spec_hash,
    validate_twin,
)
from agenttwin_simulation.runner import PRODUCER, completed_event
from agenttwin_simulation.store import SCHEMA, Row, Scope, Store, decode_cursor, encode_cursor
from agenttwin_simulation.twin.adapters import AdapterRegistry
from agenttwin_simulation.twin.definition import TwinDefinition
from agenttwin_simulation.twin.faults import FAULT_TYPES

__all__ = ["ENGINE_VERSION", "MAX_CASES", "SimulationAPI"]

ENGINE_VERSION = "twin-engine/1.0.0"
MAX_CASES = 500
_NAME = r"^[a-z0-9][a-z0-9_-]{0,98}$"
_AGENT = r"^[a-z0-9][a-z0-9_-]{0,62}$"
_TAG = re.compile(r"^[a-z0-9][a-z0-9_:.-]{0,62}$")
_RUN_STATUSES = frozenset(
    {"QUEUED", "PREPARING", "RUNNING", "EVALUATING", "COMPLETED", "FAILED", "CANCELLED"}
)
_SEVERITIES = frozenset({"critical", "high", "medium", "low"})


# ---------------------------------------------------------------- request bodies


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentBody(_Strict):
    project_id: str
    document: dict[str, Any] | None = None
    yaml: str | None = Field(default=None, max_length=600_000)

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.document is not None:
            out["document"] = self.document
        if self.yaml is not None:
            out["yaml"] = self.yaml
        return out


class RunBody(_Strict):
    project_id: str
    agent: str = Field(pattern=_AGENT)
    agent_version: str = Field(min_length=1, max_length=100)
    scenarios: list[str] | None = Field(default=None, max_length=MAX_CASES)
    tags: list[str] | None = Field(default=None, max_length=50)
    seed: int | None = Field(default=None, ge=0, le=4_294_967_295)
    eval_run_id: str | None = None
    side: Literal["SINGLE", "BASELINE", "CANDIDATE"] = "SINGLE"
    release_id: str | None = Field(default=None, max_length=200)


# ---------------------------------------------------------------- rendering


def _ts(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return value


def _pick(row: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    return {k: _ts(row.get(k)) for k in keys}


_RUN_KEYS = (
    "id",
    "organization_id",
    "project_id",
    "agent_name",
    "agent_version",
    "agent_version_id",
    "side",
    "eval_run_id",
    "release_id",
    "status",
    "requested_by",
    "cancel_requested",
    "attempts",
    "case_count",
    "passed",
    "failed",
    "errored",
    "cancelled",
    "critical_failures",
    "error",
    "created_at",
    "started_at",
    "finished_at",
    "updated_at",
)
_CASE_KEYS = (
    "id",
    "run_id",
    "position",
    "scenario_id",
    "scenario_version_id",
    "scenario_name",
    "severity",
    "twin_definition_id",
    "status",
    "seed",
    "tenant",
    "call_count",
    "trace_id",
    "reason",
    "error",
    "latency_ms",
    "outcome_status",
    "started_at",
    "finished_at",
)


def run_json(row: Mapping[str, Any], *, pinning: bool = False) -> dict[str, Any]:
    out = _pick(row, _RUN_KEYS)
    done = sum(int(row.get(k) or 0) for k in ("passed", "failed", "errored", "cancelled"))
    out["finished_cases"] = done
    if pinning:
        out["pinning"] = row.get("pinning") or {}
    return out


def case_json(row: Mapping[str, Any]) -> dict[str, Any]:
    out = _pick(row, _CASE_KEYS)
    verdict = row.get("verdict")
    out["labels"] = list((verdict or {}).get("labels") or [])
    out["score"] = (verdict or {}).get("score")
    return out


def scenario_json(row: Mapping[str, Any]) -> dict[str, Any]:
    out = _pick(
        row,
        (
            "id",
            "organization_id",
            "project_id",
            "name",
            "agent",
            "twin",
            "severity",
            "tags",
            "source",
            "latest_version",
            "archived",
            "created_by",
            "created_at",
            "updated_at",
            "version_id",
            "spec_hash",
            "description",
            "expectation_count",
            "fault_count",
        ),
    )
    return {k: v for k, v in out.items() if v is not None or k in ("agent", "twin")}


def _mask_secrets(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Twin documents list canary values; they are not shown back."""
    out = json.loads(json.dumps(doc))
    spec = out.get("spec")
    if isinstance(spec, dict) and isinstance(spec.get("secrets"), list):
        spec["secrets"] = [f"<redacted canary: {len(str(s))} characters>" for s in spec["secrets"]]
    return dict(out)


def tools_json(definition: TwinDefinition) -> list[dict[str, Any]]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "risk": t.risk,
            "handler": t.handler.kind,
            "mutates": t.expects_mutation,
            "idempotency": t.idempotency_argument
            or (f"header:{t.idempotency_header}" if t.idempotency_header else None),
            "tenant_scoped": bool(t.tenant_path),
        }
        for t in sorted(definition.tools.values(), key=lambda t: t.name)
    ]


def twin_json(row: Mapping[str, Any]) -> dict[str, Any]:
    return _pick(
        row,
        (
            "id",
            "organization_id",
            "project_id",
            "name",
            "version",
            "spec_hash",
            "tool_count",
            "description",
            "created_by",
            "created_at",
        ),
    )


def _redact(value: Any, canaries: Sequence[str], depth: int = 0) -> Any:
    """Replaces planted canary values wherever they appear (a leaked secret is
    evidence, but the value itself is never displayed)."""
    if not canaries or depth > 40:
        return value
    if isinstance(value, str):
        for c in canaries:
            if c and c in value:
                value = value.replace(c, "[REDACTED:canary]")
        return value
    if isinstance(value, Mapping):
        return {k: _redact(v, canaries, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, canaries, depth + 1) for v in value]
    return value


def _name_cursor(name: str, project: str) -> str:
    raw = json.dumps({"n": name, "p": project}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_name_cursor(token: str) -> tuple[str, str] | None:
    if not token:
        return None
    try:
        if len(token) > 512:
            raise ValueError
        raw = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        name, project = str(raw["n"]), str(raw["p"])
        if not valid_uuid(project):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise errors.invalid("INVALID_CURSOR", "Cursor is malformed.") from None
    return name, project


def _check_uuid(value: str | None, field: str) -> None:
    if value is not None and not valid_uuid(value):
        raise errors.invalid("INVALID_PARAMETER", f"{field} must be a UUID.", {"field": field})


def _accessible(p: Principal, row: Mapping[str, Any] | None) -> bool:
    return (
        row is not None
        and str(row["organization_id"]) == p.org_id
        and p.can_access_project(str(row["project_id"]))
    )


def _scope(p: Principal, project_id: str | None) -> Scope:
    if project_id:
        _check_uuid(project_id, "project_id")
        if not p.can_access_project(project_id):
            raise errors.not_found()
        return Scope(p.org_id, (project_id,))
    if p.all_projects:
        return Scope(p.org_id, None)
    return Scope(p.org_id, tuple(p.project_ids))


async def _announce(conn: Conn, run: Row) -> None:
    """A queued run cancelled on request finishes at once: its completion
    event is written in the same transaction."""
    await write_outbox(conn, SCHEMA, completed_event(run))


def _problem(code: str, message: str, check: ScenarioCheck | list[str]) -> APIError:
    if isinstance(check, ScenarioCheck):
        details: dict[str, Any] = {"problems": check.problems, "warnings": check.warnings}
    else:
        details = {"problems": check}
    return errors.invalid(code, message, details)


# ---------------------------------------------------------------- the API


class SimulationAPI:
    def __init__(
        self,
        *,
        store: Store,
        cfg: SimulationConfig,
        registry: Registry,
        control_plane: ControlPlaneClient,
        log: Log,
        adapters: AdapterRegistry | None = None,
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.registry = registry
        self.control_plane = control_plane
        self.log = log
        self.adapters = adapters or AdapterRegistry()

    # -- shared pieces ------------------------------------------------------

    def _load_twin(self, row: Mapping[str, Any]) -> TwinDefinition:
        return validate_twin(row["document"], self.adapters.names())

    async def _check(self, project: str, doc: Mapping[str, Any]) -> tuple[ScenarioCheck, Row | None]:
        """A scenario checked on its own and against the latest twin it names."""
        check = check_scenario(doc, self.registry)
        if not check.valid:
            return check, None
        twin_name = str(doc["spec"].get("twin") or "")
        twin = await self.store.latest_twin(project, twin_name) if twin_name else None
        if twin_name and twin is None:
            check.problems.append(
                f"spec.twin: there is no twin named {twin_name!r} in this project "
                "(register it first: POST /api/v1/twins)"
            )
            return check, None
        if twin is not None:
            cross = scenario_twin_problems(doc, self._load_twin(twin))
            check.problems.extend(cross.problems)
            check.warnings.extend(cross.warnings)
        return check, twin

    async def _scenario_or_404(self, p: Principal, scenario_id: str, version: int | None = None) -> Row:
        _check_uuid(scenario_id, "scenario_id")
        row = await self.store.get_scenario(scenario_id, version)
        if row is None or not _accessible(p, row):
            raise errors.not_found()
        return row

    async def _run_or_404(self, p: Principal, run_id: str) -> Row:
        _check_uuid(run_id, "run_id")
        row = await self.store.get_run(run_id)
        if row is None or not _accessible(p, row):
            raise errors.not_found()
        return row

    # -- routes ---------------------------------------------------------------

    def routes(self, app: FastAPI) -> None:
        @app.post("/api/v1/twins")
        async def register_twin(request: Request) -> JSONResponse:
            body = await read_model(request, DocumentBody, max_bytes=700_000)
            _check_uuid(body.project_id, "project_id")
            p = require_project(request, Permission.SCENARIO_WRITE, body.project_id)
            try:
                doc = parse_document(body.payload())
                definition = validate_twin(doc, self.adapters.names())
            except DocumentProblem as err:
                raise _problem("TWIN_INVALID", "The twin definition is invalid.", err.problems) from None
            row, created = await self.store.upsert_twin(
                p.org_id, body.project_id, doc, spec_hash(doc), len(definition.tools), p.actor
            )
            if created:
                self.log.info(
                    "twin registered", twin=row["name"], version=row["version"], project_id=body.project_id
                )
            out = {"twin": twin_json(row) | {"tools": tools_json(definition)}, "created": created}
            return JSONResponse(out, status_code=201 if created else 200)

        @app.get("/api/v1/twins")
        async def list_twins(request: Request) -> dict[str, Any]:
            p = require(request, Permission.READ)
            scope = _scope(p, request.query_params.get("project_id"))
            return {"items": [twin_json(r) for r in await self.store.list_twins(scope)]}

        @app.get("/api/v1/twins/{twin_id}")
        async def get_twin(twin_id: str, request: Request) -> dict[str, Any]:
            p = require(request, Permission.READ)
            _check_uuid(twin_id, "twin_id")
            row = await self.store.twin_by_id(twin_id)
            if row is None or not _accessible(p, row):
                raise errors.not_found()
            definition = self._load_twin(row)
            doc = _mask_secrets(row["document"])
            return {
                "twin": twin_json(row)
                | {"tools": tools_json(definition), "tenant_key": definition.tenant_key},
                "document": doc,
                "yaml": export_yaml(doc),
                "versions": [
                    _pick(v, ("id", "version", "spec_hash", "tool_count", "created_by", "created_at"))
                    for v in await self.store.twin_versions(str(row["project_id"]), str(row["name"]))
                ],
            }

        @app.post("/api/v1/scenarios/validate")
        async def validate_scenario(request: Request) -> dict[str, Any]:
            body = await read_model(request, DocumentBody, max_bytes=700_000)
            _check_uuid(body.project_id, "project_id")
            require_project(request, Permission.READ, body.project_id)
            try:
                doc = parse_document(body.payload())
            except DocumentProblem as err:
                return {
                    "valid": False,
                    "problems": err.problems,
                    "warnings": [],
                    "spec_hash": None,
                    "twin": None,
                }
            check, twin = await self._check(body.project_id, doc)
            return {
                "valid": check.valid,
                "problems": check.problems,
                "warnings": check.warnings,
                "spec_hash": spec_hash(doc),
                "twin": twin_json(twin) if twin is not None else None,
            }

        @app.post("/api/v1/scenarios")
        async def upsert_scenario(request: Request) -> JSONResponse:
            body = await read_model(request, DocumentBody, max_bytes=700_000)
            _check_uuid(body.project_id, "project_id")
            p = require_project(request, Permission.SCENARIO_WRITE, body.project_id)
            try:
                doc = parse_document(body.payload())
            except DocumentProblem as err:
                raise _problem("SCENARIO_INVALID", "The scenario is invalid.", err.problems) from None
            check, _ = await self._check(body.project_id, doc)
            if not check.valid:
                raise _problem("SCENARIO_INVALID", "The scenario is invalid.", check)
            digest = spec_hash(doc)
            async with transaction(self.store.pool) as conn:
                sc, ver, created = await self.store.upsert_scenario(
                    conn, p.org_id, body.project_id, doc, digest, p.actor
                )
                if created:
                    spec, meta = doc["spec"], doc["metadata"]
                    await write_outbox(
                        conn,
                        SCHEMA,
                        Envelope.new(
                            "scenario.upserted.v1",
                            PRODUCER,
                            p.org_id,
                            body.project_id,
                            request_id_var.get() or None,
                            {
                                "scenario_id": str(sc["id"]),
                                "scenario_version_id": str(ver["id"]),
                                "name": sc["name"],
                                "agent": spec.get("agent"),
                                "severity": meta["severity"],
                                "tags": list(meta.get("tags") or []),
                                "covers": list(spec.get("covers") or []),
                                "spec_hash": digest,
                                "twin": spec.get("twin"),
                                "version": ver["version"],
                            },
                        ),
                    )
            if created:
                self.log.info(
                    "scenario saved", scenario=sc["name"], version=ver["version"], project_id=body.project_id
                )
            out = {
                "scenario": scenario_json(sc | {"version_id": ver["id"], "spec_hash": ver["spec_hash"]}),
                "version": ver["version"],
                "created": created,
                "warnings": check.warnings,
            }
            return JSONResponse(out, status_code=201 if created else 200)

        @app.get("/api/v1/scenarios")
        async def list_scenarios(request: Request) -> dict[str, Any]:
            p = require(request, Permission.READ)
            q = request.query_params
            scope = _scope(p, q.get("project_id"))
            severity = q.get("severity") or None
            if severity is not None and severity not in _SEVERITIES:
                raise errors.invalid("INVALID_PARAMETER", "severity is invalid.", {"field": "severity"})
            limit = query_int(q, "limit", 50, 1, 200)
            rows = await self.store.list_scenarios(
                scope,
                agent=q.get("agent") or None,
                severity=severity,
                tag=q.get("tag") or None,
                query=(q.get("q") or "")[:200] or None,
                # Exact: "is this name taken?" before saving (saving is by name).
                name=(q.get("name") or "")[:200] or None,
                include_archived=q.get("include_archived") in ("1", "true"),
                after=_decode_name_cursor(q.get("cursor") or ""),
                limit=limit + 1,
            )
            items = rows[:limit]
            nxt = _name_cursor(items[-1]["name"], str(items[-1]["project_id"])) if len(rows) > limit else None
            return {"items": [scenario_json(r) for r in items], "next_cursor": nxt}

        @app.get("/api/v1/scenarios/{scenario_id}")
        async def get_scenario(scenario_id: str, request: Request) -> dict[str, Any]:
            p = require(request, Permission.READ)
            raw_version = request.query_params.get("version")
            version = None
            if raw_version:
                version = query_int(request.query_params, "version", 1, 1, 1_000_000)
            row = await self._scenario_or_404(p, scenario_id, version)
            doc = row["document"]
            return {
                "scenario": scenario_json(row),
                "version": row["version"],
                "version_created_by": row["version_created_by"],
                "version_created_at": _ts(row["version_created_at"]),
                "document": doc,
                "yaml": export_yaml(doc),
                "versions": [
                    _pick(v, ("id", "version", "spec_hash", "created_by", "created_at"))
                    for v in await self.store.scenario_versions(scenario_id)
                ],
            }

        @app.post("/api/v1/scenarios/{scenario_id}/archive")
        async def archive_scenario(scenario_id: str, request: Request) -> dict[str, Any]:
            p = require(request, Permission.SCENARIO_WRITE)
            await self._scenario_or_404(p, scenario_id)
            row = await self.store.archive_scenario(scenario_id)
            if row is None:
                raise errors.not_found()
            return {"scenario": scenario_json(row)}

        @app.get("/api/v1/simulations/capabilities")
        async def capabilities(request: Request) -> dict[str, Any]:
            require(request, Permission.READ)
            return {
                "agents": sorted(self.cfg.agent_endpoints),
                "fault_types": list(FAULT_TYPES),
                "expectation_types": self.registry.types(),
                "evaluators": self.registry.versions(),
                "engine": ENGINE_VERSION,
                "limits": {
                    "max_cases": MAX_CASES,
                    "max_calls_per_case": self.cfg.max_calls_per_case,
                    "case_timeout_seconds": self.cfg.case_timeout_s,
                    "max_fault_delay_ms": self.cfg.max_fault_delay_ms,
                },
            }

        @app.post("/api/v1/simulations")
        async def create_run(request: Request) -> JSONResponse:
            body = await read_model(request, RunBody)
            _check_uuid(body.project_id, "project_id")
            _check_uuid(body.eval_run_id, "eval_run_id")
            p = require_project(request, Permission.SIMULATION_RUN, body.project_id)
            return await self._create_run(p, body)

        @app.get("/api/v1/simulations")
        async def list_runs(request: Request) -> dict[str, Any]:
            p = require(request, Permission.READ)
            q = request.query_params
            scope = _scope(p, q.get("project_id"))
            status = q.get("status") or None
            if status is not None and status not in _RUN_STATUSES:
                raise errors.invalid("INVALID_PARAMETER", "status is invalid.", {"field": "status"})
            eval_run_id = q.get("eval_run_id") or None
            _check_uuid(eval_run_id, "eval_run_id")
            try:
                cursor = decode_cursor(q.get("cursor") or "")
            except ValueError:
                raise errors.invalid("INVALID_CURSOR", "Cursor is malformed.") from None
            limit = query_int(q, "limit", 50, 1, 200)
            rows = await self.store.list_runs(
                scope,
                agent=q.get("agent") or None,
                status=status,
                eval_run_id=eval_run_id,
                cursor=cursor,
                limit=limit + 1,
            )
            items = rows[:limit]
            nxt = encode_cursor(items[-1]["created_at"], str(items[-1]["id"])) if len(rows) > limit else None
            return {"items": [run_json(r) for r in items], "next_cursor": nxt}

        @app.get("/api/v1/simulations/{run_id}")
        async def get_run(run_id: str, request: Request) -> dict[str, Any]:
            p = require(request, Permission.READ)
            run = await self._run_or_404(p, run_id)
            return {
                "run": run_json(run, pinning=True),
                "cases": [case_json(c) for c in await self.store.run_cases(run_id)],
                "transitions": [
                    _pick(t, ("seq", "from_status", "to_status", "reason", "at"))
                    for t in await self.store.run_transitions(run_id)
                ],
            }

        @app.get("/api/v1/simulations/{run_id}/cases/{case_id}")
        async def get_case(run_id: str, case_id: str, request: Request) -> dict[str, Any]:
            p = require(request, Permission.READ)
            await self._run_or_404(p, run_id)
            _check_uuid(case_id, "case_id")
            case = await self.store.get_case(run_id, case_id)
            if case is None:
                raise errors.not_found()
            return await self._case_detail(case)

        @app.post("/api/v1/simulations/{run_id}/cancel")
        async def cancel_run(run_id: str, request: Request) -> JSONResponse:
            p = require(request, Permission.SIMULATION_RUN)
            await self._run_or_404(p, run_id)
            row = await self.store.request_cancel(run_id, p.actor, on_terminal=_announce)
            if row is None:
                raise errors.not_found()
            self.log.info("simulation run cancellation requested", run_id=run_id, status=row["status"])
            # 202: a worker stops the run at its next checkpoint; 200: already final.
            final = JobStatus(row["status"]).terminal
            return JSONResponse({"run": run_json(row)}, status_code=200 if final else 202)

    # -- run creation -----------------------------------------------------------

    async def _create_run(self, p: Principal, body: RunBody) -> JSONResponse:
        project = body.project_id
        if body.agent not in self.cfg.agent_endpoints:
            raise errors.invalid(
                "AGENT_NOT_RUNNABLE",
                f"This deployment cannot run agent {body.agent!r}: no endpoint is configured for it "
                "(SIMULATION_AGENT_ENDPOINTS).",
                {"runnable": sorted(self.cfg.agent_endpoints)},
            )
        for tag in body.tags or []:
            if not _TAG.match(tag):
                raise errors.invalid("INVALID_PARAMETER", f"Invalid tag {tag!r}.", {"field": "tags"})
        for name in body.scenarios or []:
            if not re.match(_NAME, name):
                raise errors.invalid(
                    "INVALID_PARAMETER", f"Invalid scenario name {name!r}.", {"field": "scenarios"}
                )
        try:
            version = await self.control_plane.agent_version(
                p.org_id, project, body.agent, body.agent_version
            )
        except UpstreamError as err:
            self.log.warn("agent version lookup failed", error=str(err))
            raise errors.unavailable(
                "The control plane could not be reached to resolve the agent version."
            ) from None
        if version is None:
            raise errors.invalid(
                "AGENT_VERSION_NOT_FOUND",
                f"Agent {body.agent!r} has no registered version {body.agent_version!r} in this project.",
            )
        rows = await self.store.scenarios_for_run(project, body.agent, body.scenarios, body.tags)
        if body.scenarios:
            missing = sorted(set(body.scenarios) - {r["name"] for r in rows})
            if missing:
                raise errors.invalid(
                    "SCENARIO_NOT_FOUND",
                    "Some requested scenarios do not exist for this agent (or are archived).",
                    {"missing": missing},
                )
        if not rows:
            raise errors.invalid(
                "NO_SCENARIOS", "No scenario of this project applies to the agent and filters."
            )
        if len(rows) > MAX_CASES:
            raise errors.invalid("TOO_MANY_SCENARIOS", f"A run holds at most {MAX_CASES} scenarios.")
        twins: dict[str, Row | None] = {}
        definitions: dict[str, TwinDefinition] = {}
        problems: dict[str, list[str]] = {}
        for r in rows:
            doc = r["document"]
            twin_name = str(doc["spec"].get("twin") or "")
            if twin_name not in twins:
                twins[twin_name] = await self.store.latest_twin(project, twin_name) if twin_name else None
            twin = twins[twin_name]
            if twin is None:
                problems[r["name"]] = [f"spec.twin: there is no twin named {twin_name!r} in this project"]
                continue
            definition = definitions.get(twin_name)
            if definition is None:
                definition = definitions[twin_name] = self._load_twin(twin)
            cross = scenario_twin_problems(doc, definition)
            if cross.problems:
                problems[r["name"]] = cross.problems
        if problems:
            raise errors.invalid(
                "SCENARIO_INVALID",
                "Some scenarios cannot run against their twins.",
                {"scenarios": problems},
            )
        run_seed = body.seed if body.seed is not None else secrets.randbits(32)
        run_id = new_id()
        correlation = request_id_var.get() or run_id
        cases: list[dict[str, Any]] = []
        pinned: list[dict[str, Any]] = []
        for i, r in enumerate(rows):
            doc = r["document"]
            twin = twins[str(doc["spec"].get("twin") or "")]
            assert twin is not None  # noqa: S101 - checked above
            seed = case_seed(run_seed, r["name"], doc["spec"].get("seed"))
            cases.append(
                {
                    "id": new_id(),
                    "position": i,
                    "scenario_id": r["id"],
                    "scenario_version_id": r["version_id"],
                    "scenario_name": r["name"],
                    "severity": r["severity"],
                    "twin_definition_id": twin["id"],
                    "seed": seed,
                    "tenant": case_tenant(doc),
                }
            )
            pinned.append(
                {
                    "scenario": r["name"],
                    "scenario_version_id": str(r["version_id"]),
                    "spec_hash": r["spec_hash"],
                    "twin": twin["name"],
                    "twin_definition_id": str(twin["id"]),
                    "twin_version": twin["version"],
                    "twin_spec_hash": twin["spec_hash"],
                    "seed": seed,
                }
            )
        pinning = {
            "correlation_id": correlation,
            "seed": run_seed,
            "agent": {
                "name": body.agent,
                "version": body.agent_version,
                "version_id": version.get("id"),
                "manifest_sha256": version.get("manifest_sha256"),
                "prompt_sha256": version.get("prompt_sha256"),
                "model_provider": version.get("model_provider"),
                "model_name": version.get("model_name"),
                "commit_sha": version.get("commit_sha"),
            },
            "scenarios": pinned,
            "evaluators": self.registry.versions(),
            "engine": ENGINE_VERSION,
            "selection": {"scenarios": body.scenarios, "tags": body.tags},
        }
        run = {
            "id": run_id,
            "organization_id": p.org_id,
            "project_id": project,
            "agent_name": body.agent,
            "agent_version": body.agent_version,
            "agent_version_id": version.get("id"),
            "side": body.side,
            "eval_run_id": body.eval_run_id,
            "release_id": body.release_id,
            "requested_by": p.actor,
            "pinning": pinning,
        }
        async with transaction(self.store.pool) as conn:
            row = await self.store.insert_run(conn, run, cases)
            await write_outbox(
                conn,
                SCHEMA,
                Envelope.new(
                    "simulation.run_requested.v1",
                    PRODUCER,
                    p.org_id,
                    project,
                    correlation,
                    {"run_id": run_id, "eval_run_id": body.eval_run_id, "side": body.side},
                ),
            )
        self.log.info(
            "simulation run requested",
            run_id=run_id,
            agent=body.agent,
            version=body.agent_version,
            cases=len(cases),
            seed=run_seed,
        )
        return JSONResponse(
            {
                "run": run_json(row, pinning=True),
                "cases": [
                    _pick(c, ("id", "position", "scenario_id", "scenario_name", "severity", "seed", "tenant"))
                    for c in cases
                ],
            },
            status_code=202,
        )

    async def _case_detail(self, case: Row) -> dict[str, Any]:
        twin_row = await self.store.twin_by_id(str(case["twin_definition_id"]))
        canaries: list[str] = []
        twin_info = None
        if twin_row is not None:
            canaries = [str(s) for s in ((twin_row["document"].get("spec") or {}).get("secrets") or [])]
            twin_info = _pick(twin_row, ("id", "name", "version", "spec_hash"))
        raw_state = case.get("twin_state") or {}
        doc = case["scenario_document"]
        steps = [
            _pick(s, ("seq", "kind", "tool", "latency_ms", "created_at")) | {"record": s["record"]}
            for s in await self.store.case_steps(str(case["id"]))
        ]
        detail = {
            "case": case_json(case)
            | {
                "verdict": case.get("verdict"),
                "results": case.get("results") or [],
                "state_diff": case.get("state_diff") or [],
                "agent_result": case.get("agent_result"),
            },
            "scenario": {"document": doc, "faults": doc["spec"].get("faults") or []},
            "twin": twin_info,
            "steps": steps,
            "state": {"initial": raw_state.get("initial"), "final": raw_state.get("state")},
        }
        return dict(_redact(detail, canaries))
