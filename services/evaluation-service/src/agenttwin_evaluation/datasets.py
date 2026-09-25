"""Datasets (spec §17): named, versioned collections of the project's
scenarios, with the metadata the spec asks of an eval case — tags, source,
the production trace a case came from, its privacy status and a note.

POST   /api/v1/datasets                              create (version 1)
GET    /api/v1/datasets                              list
GET    /api/v1/datasets/{id}[?version=N]             one version, with each case's latest result
POST   /api/v1/datasets/{id}/cases                   add or update cases (a new version)
DELETE /api/v1/datasets/{id}/cases/{scenario}        remove a case (a new version)
POST   /api/v1/datasets/{id}/archive                 archive

A version is an immutable snapshot of its cases: an evaluation run pins the
version it ran, so its selection can always be read back. Every change is
announced to the audit log.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
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
from agenttwin_evaluation.clients import SimulationClient, UpstreamError
from agenttwin_evaluation.common import (
    MAX_CASES,
    NAME,
    TAG,
    Strict,
    accessible,
    audit,
    check_uuid,
    decode_name_cursor,
    name_cursor,
    pick,
    scope_of,
    ts,
)
from agenttwin_evaluation.store import Row, Store

__all__ = ["DatasetsAPI", "dataset_json"]

Source = Literal["manual", "production_regression", "generated", "policy", "imported"]
_CASE_FIELDS = ("scenario", "tags", "source", "trace_id", "privacy", "note")


class DatasetCaseBody(Strict):
    scenario: str = Field(pattern=NAME)
    tags: list[str] | None = Field(default=None, max_length=20)
    source: Source = "manual"
    trace_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    privacy: Literal["synthetic", "redacted"] = "synthetic"
    note: str | None = Field(default=None, max_length=2000)


class CreateDatasetBody(Strict):
    project_id: str
    name: str = Field(pattern=NAME)
    description: str | None = Field(default=None, max_length=2000)
    owner: str | None = Field(default=None, max_length=200)
    tags: list[str] | None = Field(default=None, max_length=20)
    cases: list[DatasetCaseBody] = Field(default_factory=list, max_length=MAX_CASES)


class AddCasesBody(Strict):
    cases: list[DatasetCaseBody] = Field(min_length=1, max_length=MAX_CASES)
    note: str | None = Field(default=None, max_length=500)


_DATASET_KEYS = (
    "id",
    "organization_id",
    "project_id",
    "name",
    "description",
    "owner",
    "tags",
    "latest_version",
    "archived",
    "created_by",
    "created_at",
    "updated_at",
)


def dataset_json(row: Mapping[str, Any]) -> dict[str, Any]:
    out = pick(row, _DATASET_KEYS)
    out["tags"] = list(row.get("tags") or [])
    return out


def _version_json(row: Mapping[str, Any]) -> dict[str, Any]:
    return pick(row, ("version", "case_count", "note", "created_by", "created_at"))


def _last_result_json(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return pick(
        row,
        (
            "eval_run_id",
            "classification",
            "baseline_version",
            "candidate_version",
            "baseline_status",
            "candidate_status",
            "finished_at",
        ),
    )


def _tags(values: Sequence[str] | None, field: str) -> list[str]:
    out = sorted(set(values or []))
    for tag in out:
        if not TAG.match(tag):
            raise errors.invalid("INVALID_PARAMETER", f"Invalid tag {tag!r}.", {"field": field})
    return out


def _case(body: DatasetCaseBody, actor: str, now: str) -> dict[str, Any]:
    if body.source == "production_regression" and body.privacy != "redacted":
        raise errors.invalid(
            "CASE_NOT_REDACTED",
            f"Case {body.scenario!r} comes from production; it must be redacted before it joins a dataset.",
            {"scenario": body.scenario},
        )
    return {
        "scenario": body.scenario,
        "tags": _tags(body.tags, "cases.tags"),
        "source": body.source,
        "trace_id": body.trace_id,
        "privacy": body.privacy,
        "note": body.note,
        "added_by": actor,
        "added_at": now,
    }


def _no_duplicates(cases: Sequence[DatasetCaseBody]) -> None:
    seen: set[str] = set()
    for c in cases:
        if c.scenario in seen:
            raise errors.invalid(
                "INVALID_PARAMETER", f"Scenario {c.scenario!r} is listed twice.", {"field": "cases"}
            )
        seen.add(c.scenario)


def _same(a: Sequence[Mapping[str, Any]], b: Sequence[Mapping[str, Any]]) -> bool:
    """Equal case lists, ignoring who added a case and when."""

    def core(cases: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [{k: c.get(k) for k in _CASE_FIELDS} for c in cases]

    return core(a) == core(b)


def _cases_hash(cases: Sequence[Mapping[str, Any]]) -> str:
    return content_hash([{k: c.get(k) for k in _CASE_FIELDS} for c in cases])


class DatasetsAPI:
    def __init__(self, *, store: Store, simulation: SimulationClient, log: Log) -> None:
        self.store = store
        self.simulation = simulation
        self.log = log

    async def _dataset_or_404(self, p: Principal, dataset_id: str) -> Row:
        check_uuid(dataset_id, "dataset_id")
        row = await self.store.get_dataset(dataset_id)
        if row is None or not accessible(p, row):
            raise errors.not_found()
        return row

    async def _check_scenarios(self, p: Principal, project: str, names: Sequence[str]) -> None:
        """Every case must name a scenario of the project that is not archived."""
        if not names:
            return
        try:
            known = {str(s.get("name")) for s in await self.simulation.scenarios(p.org_id, project)}
        except UpstreamError as err:
            self.log.warn("scenario lookup failed", error=str(err))
            raise errors.unavailable(
                "The simulation service could not be reached to check the scenarios."
            ) from None
        missing = sorted(set(names) - known)
        if missing:
            raise errors.invalid(
                "SCENARIO_NOT_FOUND",
                "Some cases name scenarios that do not exist in this project (or are archived).",
                {"missing": missing},
            )

    async def detail(self, dataset: Row, version: int | None = None) -> dict[str, Any]:
        wanted = int(dataset["latest_version"]) if version is None else version
        v = await self.store.dataset_version(str(dataset["id"]), wanted)
        if v is None:
            raise errors.not_found("The dataset has no such version.")
        last = await self.store.last_results(str(dataset["id"]))
        cases = [
            dict(c) | {"last_result": _last_result_json(last.get(str(c["scenario"])))} for c in v["cases"]
        ]
        return {
            "dataset": dataset_json(dataset),
            "version": _version_json(v) | {"cases": cases},
            "versions": [_version_json(r) for r in await self.store.dataset_versions(str(dataset["id"]))],
        }

    def routes(self, app: FastAPI) -> None:
        @app.post("/api/v1/datasets")
        async def create_dataset(request: Request) -> JSONResponse:
            body = await read_model(request, CreateDatasetBody)
            check_uuid(body.project_id, "project_id")
            p = require_project(request, Permission.SCENARIO_WRITE, body.project_id)
            _no_duplicates(body.cases)
            now = ts(datetime.now(UTC))
            cases = [_case(c, p.actor, now) for c in body.cases]
            tags = _tags(body.tags, "tags")
            await self._check_scenarios(p, body.project_id, [c["scenario"] for c in cases])
            dataset_id = new_id()
            async with transaction(self.store.pool) as conn:
                row = await self.store.insert_dataset(
                    conn,
                    {
                        "id": dataset_id,
                        "organization_id": p.org_id,
                        "project_id": body.project_id,
                        "name": body.name,
                        "description": body.description,
                        "owner": body.owner,
                        "tags": tags,
                        "created_by": p.actor,
                    },
                    {"id": new_id(), "cases": cases, "note": "created", "created_by": p.actor},
                )
                if row is None:
                    raise errors.conflict(
                        "DATASET_EXISTS", f"This project already has a dataset named {body.name!r}."
                    )
                await audit(
                    conn,
                    p,
                    body.project_id,
                    "dataset.create",
                    "dataset",
                    dataset_id,
                    after_hash=_cases_hash(cases),
                    metadata={"name": body.name, "version": 1, "cases": len(cases)},
                )
            self.log.info("dataset created", dataset_id=dataset_id, cases=len(cases))
            return JSONResponse(await self.detail(row), status_code=201)

        @app.get("/api/v1/datasets")
        async def list_datasets(request: Request) -> dict[str, Any]:
            p = require(request, Permission.READ)
            q = request.query_params
            scope = scope_of(p, q.get("project_id") or None)
            include_archived = (q.get("include_archived") or "").lower() in ("1", "true")
            after = decode_name_cursor(q.get("cursor") or "")
            limit = query_int(q, "limit", 50, 1, 200)
            rows = await self.store.list_datasets(
                scope,
                include_archived=include_archived,
                query=q.get("q") or None,
                after=after,
                limit=limit + 1,
            )
            items = rows[:limit]
            nxt = name_cursor(str(items[-1]["name"]), str(items[-1]["id"])) if len(rows) > limit else None
            return {
                "items": [dataset_json(r) | {"case_count": int(r["case_count"])} for r in items],
                "next_cursor": nxt,
            }

        @app.get("/api/v1/datasets/{dataset_id}")
        async def get_dataset(dataset_id: str, request: Request) -> dict[str, Any]:
            p = require(request, Permission.READ)
            row = await self._dataset_or_404(p, dataset_id)
            version = request.query_params.get("version")
            wanted = (
                None if version in (None, "") else query_int(request.query_params, "version", 1, 1, 1_000_000)
            )
            return await self.detail(row, wanted)

        @app.post("/api/v1/datasets/{dataset_id}/cases")
        async def add_cases(dataset_id: str, request: Request) -> JSONResponse:
            p = require(request, Permission.SCENARIO_WRITE)
            dataset = await self._dataset_or_404(p, dataset_id)
            body = await read_model(request, AddCasesBody)
            _no_duplicates(body.cases)
            now = ts(datetime.now(UTC))
            incoming = [_case(c, p.actor, now) for c in body.cases]
            await self._check_scenarios(p, str(dataset["project_id"]), [c["scenario"] for c in incoming])

            def merge(current: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
                by_name = {str(c["scenario"]): dict(c) for c in current}
                for c in incoming:
                    old = by_name.get(c["scenario"])
                    if old is not None and all(old.get(k) == c.get(k) for k in _CASE_FIELDS):
                        continue  # unchanged: keep who added it and when
                    by_name[c["scenario"]] = c
                order = [str(c["scenario"]) for c in current]
                order += [c["scenario"] for c in incoming if c["scenario"] not in order]
                return [by_name[name] for name in order]

            return await self._change(
                p, dataset, merge, body.note or f"added or updated {len(incoming)} case(s)"
            )

        @app.delete("/api/v1/datasets/{dataset_id}/cases/{scenario}")
        async def remove_case(dataset_id: str, scenario: str, request: Request) -> JSONResponse:
            p = require(request, Permission.SCENARIO_WRITE)
            dataset = await self._dataset_or_404(p, dataset_id)
            if not re.match(NAME, scenario):
                raise errors.invalid(
                    "INVALID_PARAMETER", "scenario is not a scenario name.", {"field": "scenario"}
                )
            latest = await self.store.dataset_version(dataset_id, int(dataset["latest_version"]))
            if latest is None or all(c["scenario"] != scenario for c in latest["cases"]):
                raise errors.not_found("The dataset has no case for this scenario.")
            return await self._change(
                p,
                dataset,
                lambda current: [dict(c) for c in current if c["scenario"] != scenario],
                f"removed {scenario}",
                created=200,
            )

        @app.post("/api/v1/datasets/{dataset_id}/archive")
        async def archive_dataset(dataset_id: str, request: Request) -> dict[str, Any]:
            p = require(request, Permission.SCENARIO_WRITE)
            dataset = await self._dataset_or_404(p, dataset_id)
            if dataset["archived"]:
                return {"dataset": dataset_json(dataset)}
            async with transaction(self.store.pool) as conn:
                row = await self.store.archive_dataset(conn, dataset_id)
                await audit(conn, p, str(dataset["project_id"]), "dataset.archive", "dataset", dataset_id)
            assert row is not None  # noqa: S101 - the dataset exists
            return {"dataset": dataset_json(row)}

    async def _change(
        self, p: Principal, dataset: Row, change: Any, note: str, *, created: int = 201
    ) -> JSONResponse:
        """A new version from the latest one (answered with ``created``);
        ``200`` with the latest version when nothing changed. Changes to one
        dataset are serialized on its row, so each applies to the version the
        one before it made: none is refused or lost."""
        dataset_id = str(dataset["id"])
        async with transaction(self.store.pool) as conn:
            locked = await self.store.lock_dataset(conn, dataset_id)
            assert locked is not None  # noqa: S101 - datasets are never deleted
            if locked["archived"]:
                raise errors.conflict("DATASET_ARCHIVED", "The dataset is archived; it can no longer change.")
            latest = await self.store.version_in(conn, dataset_id, int(locked["latest_version"]))
            assert latest is not None  # noqa: S101 - the latest version always exists
            cases = change(latest["cases"])
            if _same(cases, latest["cases"]):
                row = None
            else:
                if len(cases) > MAX_CASES:
                    raise errors.invalid("TOO_MANY_CASES", f"A dataset holds at most {MAX_CASES} cases.")
                row = await self.store.add_dataset_version(
                    conn, dataset_id, {"id": new_id(), "cases": cases, "note": note, "created_by": p.actor}
                )
                await audit(
                    conn,
                    p,
                    str(dataset["project_id"]),
                    "dataset.update",
                    "dataset",
                    dataset_id,
                    reason=note,
                    before_hash=_cases_hash(latest["cases"]),
                    after_hash=_cases_hash(cases),
                    metadata={"version": int(row["latest_version"]), "cases": len(cases)},
                )
        if row is None:
            return JSONResponse(await self.detail(locked), status_code=200)
        self.log.info("dataset changed", dataset_id=dataset_id, version=row["latest_version"])
        return JSONResponse(await self.detail(row), status_code=created)
