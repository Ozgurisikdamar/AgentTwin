"""The regression miner at work (ADR-0032): trace events in, groups out.

``trace.ingested.v1`` carries a finalized trace's summary and signals;
``trace.outcome_recorded.v1`` and ``trace.flagged.v1`` say something new
about a trace, so its detail is read again from the trace service (the
authority on it). Each event is applied once (``processed_event``), in one
transaction:

1. the observation of the trace (flags received before it was finalized are
   kept in ``regression_flag`` and added);
2. no failure signal: nothing, or, if the trace was mined before and is no
   longer a failure (its outcome was corrected), it leaves its group;
3. otherwise the rules suggest its label and severity, and its occurrence is
   stored in its group: the one it is in if its fingerprint did not change,
   else the group of its fingerprint, else the group of its nearest
   neighbour (same project, agent and embedding model) if close enough,
   else a new group of one. A fixed group that fails again in its fixed
   version or a later one is reopened.

Mining for one project's agent is serialized (an advisory lock), so two
failures of a new kind make one group.

An evaluation run whose candidate passes a promoted group's scenario fixes
the group (:meth:`Miner.record_fixes`, in the run's completing transaction).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from agenttwin_core.db import Conn, Pool, transaction
from agenttwin_core.embeddings import EmbeddingError, EmbeddingProvider, is_zero, vector_literal
from agenttwin_core.events import Envelope, Permanent, claim_event
from agenttwin_core.ids import new_id
from agenttwin_core.logx import Log
from agenttwin_evaluation.clients import TraceClient
from agenttwin_evaluation.mining import (
    Observation,
    choose_group,
    component,
    detect,
    feature_text,
    features,
    fingerprint,
    observation_from_detail,
    observation_from_event,
    on_occurrence,
    suggest_severity,
    suggest_taxonomy,
    title,
    transition,
    with_flag,
)
from agenttwin_evaluation.regression_store import MINER_ACTOR, RegressionStore

__all__ = ["CONSUMER", "EVALUATION_ACTOR", "TRACE_EVENTS", "Mined", "Miner"]

CONSUMER = "evaluation-service.regression-miner"
EVALUATION_ACTOR = "system:evaluation"
TRACE_EVENTS = frozenset({"trace.ingested.v1", "trace.outcome_recorded.v1", "trace.flagged.v1"})

Outcome = Literal[
    "ignored",  # not a production trace, or no longer known to the trace service
    "duplicate",  # the event was applied before
    "waiting",  # the trace is not finalized yet; its ingestion will mine it
    "not_candidate",  # no failure signal
    "withdrawn",  # mined before, no longer a failure
    "created",  # a new group
    "joined",  # an existing group
    "updated",  # what was known about it changed; same group
]


@dataclass(frozen=True)
class Mined:
    """What applying one trace event did."""

    trace_id: str
    outcome: Outcome
    group_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class _Vector:
    model: str
    dims: int
    literal: str | None  # None: nothing to compare


@dataclass
class Miner:
    store: RegressionStore
    traces: TraceClient
    embedder: EmbeddingProvider
    threshold: float
    log: Log

    @property
    def pool(self) -> Pool:
        return self.store.pool

    # ------------------------------------------------------------ events

    async def on_event(self, env: Envelope) -> Mined | None:
        """Applies a trace event (other events: None)."""
        if env.type not in TRACE_EVENTS:
            return None
        project = env.project_id
        payload = env.payload
        trace_id = str(payload.get("trace_id") or "")
        if not project or not trace_id:
            raise Permanent(f"{env.type} names no project or trace")
        org = env.organization_id
        flag: tuple[str, str, str | None] | None = None
        finalized = True
        if env.type == "trace.ingested.v1":
            obs: Observation | None = observation_from_event(payload)
        else:
            detail = await self.traces.trace(org, project, trace_id)
            obs = observation_from_detail(detail) if detail is not None else None
            finalized = bool(detail and detail["trace"].get("finalized"))
            if env.type == "trace.flagged.v1":
                flag = (
                    str(payload.get("kind") or "manual"),
                    str(payload.get("reason") or "").strip()[:2000],
                    str(payload["flagged_by"]) if payload.get("flagged_by") else None,
                )
        if obs is None or not obs.production:
            return Mined(trace_id, "ignored")
        async with transaction(self.pool) as conn:
            if not await claim_event(conn, CONSUMER, env.id):
                return Mined(trace_id, "duplicate")
            if flag is not None and flag[1]:
                await self.store.add_flag(
                    conn,
                    event_id=env.id,
                    project_id=project,
                    trace_id=trace_id,
                    kind=flag[0],
                    reason=flag[1],
                    flagged_by=flag[2],
                )
            if not finalized:
                return Mined(trace_id, "waiting", reason="the trace is not finalized yet")
            for kind, reason in await self.store.flags_of(conn, project, trace_id):
                obs = with_flag(obs, kind, reason)
            mined = await self.mine(conn, org, project, obs, occurred_at=env.occurred_at)
        self.log.info(
            "trace mined",
            trace_id=trace_id,
            outcome=mined.outcome,
            regression_group_id=mined.group_id,
            event=env.type,
        )
        return mined

    # ------------------------------------------------------------ mining

    async def _vector(self, obs: Observation, taxonomy: Sequence[str]) -> _Vector:
        text = feature_text(features(obs), taxonomy)
        try:
            [vec] = await self.embedder.embed([text])
        except EmbeddingError as err:
            if err.retryable:
                raise
            self.log.warn("regression features not embedded", trace_id=obs.trace_id, error=str(err))
            return _Vector(self.embedder.model, self.embedder.dims, None)
        return _Vector(self.embedder.model, self.embedder.dims, None if is_zero(vec) else vector_literal(vec))

    async def mine(self, conn: Conn, org: str, project: str, obs: Observation, *, occurred_at: str) -> Mined:
        """Stores (or withdraws) the trace's occurrence and groups it."""
        reasons = detect(obs)
        await self.store.lock_agent(conn, project, obs.agent)
        existing = await self.store.occurrence(conn, project, obs.trace_id)
        if not reasons:
            if existing is None:
                return Mined(obs.trace_id, "not_candidate")
            old = str(existing["group_id"])
            await self.store.delete_occurrence(conn, project, obs.trace_id)
            await self.store.refresh_group(conn, old)
            await self.store.drop_if_untouched(conn, old)
            return Mined(obs.trace_id, "withdrawn", old, "it no longer shows a failure")

        s = suggest_taxonomy(obs)
        severity, severity_reason = suggest_severity(obs, s)
        fp = fingerprint(obs, s)
        vector = await self._vector(obs, s.all)

        group: dict[str, Any] | None = None
        if existing is not None and existing["fingerprint"] == fp:
            group = await self.store.live_group(conn, str(existing["group_id"]))
        if group is not None and existing is not None:
            kind, why, similarity = (
                str(existing["join_kind"]),
                str(existing["join_reason"]),
                existing["similarity"],
            )
        else:
            exact = await self.store.group_of_fingerprint(conn, project, obs.agent, fp)
            nearest = None
            if exact is None and vector.literal is not None:
                nearest = await self.store.nearest(
                    conn,
                    organization_id=org,
                    project_id=project,
                    agent=obs.agent,
                    model=vector.model,
                    dims=vector.dims,
                    vector=vector.literal,
                    exclude_trace=obs.trace_id,
                )
            choice = choose_group(
                exact_group=str(exact["id"]) if exact else None,
                nearest_group=nearest[0] if nearest else None,
                nearest_similarity=nearest[1] if nearest else None,
                threshold=self.threshold,
            )
            kind, why, similarity = choice.kind, choice.reason, choice.similarity
            if choice.kind == "exact":
                group = exact
            elif choice.kind == "similar" and choice.group_id:
                group = await self.store.live_group(conn, choice.group_id)
                if group is not None:
                    await self.store.map_fingerprint(conn, project, obs.agent, fp, group["id"])

        seen_at = obs.started_at or occurred_at
        name = title(obs, s)
        tool = component(obs, s)
        created = group is None
        if group is None:
            kind = "new"
            group = await self.store.create_group(
                conn,
                {
                    "id": new_id(),
                    "organization_id": org,
                    "project_id": project,
                    "agent_name": obs.agent,
                    "fingerprint": fp,
                    "title": name,
                    "taxonomy": s.primary,
                    "secondary": s.secondary,
                    "severity": severity,
                    "severity_reason": severity_reason,
                    "evidence": s.evidence,
                    "component": tool,
                    "representative_trace_id": obs.trace_id,
                    "seen_at": seen_at,
                },
            )
            await self.store.add_event(
                conn,
                str(group["id"]),
                action="created",
                actor=MINER_ACTOR,
                to_status="CANDIDATE",
                reason=why,
                detail={"trace_id": obs.trace_id, "fingerprint": fp},
            )
        gid = str(group["id"])
        moved_from = (
            str(existing["group_id"]) if existing is not None and str(existing["group_id"]) != gid else None
        )
        await self.store.save_occurrence(
            conn,
            {
                "organization_id": org,
                "project_id": project,
                "trace_id": obs.trace_id,
                "group_id": gid,
                "agent_name": obs.agent,
                "agent_version": obs.agent_version,
                "environment": obs.environment,
                "started_at": seen_at,
                "fingerprint": fp,
                "title": name,
                "component": tool,
                "taxonomy": s.primary,
                "secondary": s.secondary,
                "severity": severity,
                "severity_reason": severity_reason,
                "evidence": s.evidence,
                "reasons": reasons,
                "observation": _observation_json(obs),
                "features": features(obs),
                "join_kind": kind,
                "join_reason": why,
                "similarity": similarity,
                "model": vector.model,
                "dims": vector.dims,
                "embedding": vector.literal,
            },
        )
        if moved_from is not None:
            await self.store.refresh_group(conn, moved_from)
            await self.store.drop_if_untouched(conn, moved_from)
        refreshed = await self.store.refresh_group(conn, gid)
        assert refreshed is not None  # noqa: S101 - locked above
        joined = existing is None or moved_from is not None
        if joined and not created:
            effect = on_occurrence(str(refreshed["status"]), obs.agent_version, refreshed["fixed_version"])
            if effect.reopened:
                await self.store.set_status(conn, gid, effect.status)
                await self.store.add_event(
                    conn,
                    gid,
                    action="reopen",
                    actor=MINER_ACTOR,
                    from_status=str(refreshed["status"]),
                    to_status=effect.status,
                    reason=effect.reason,
                    detail={"trace_id": obs.trace_id, "agent_version": obs.agent_version},
                )
        outcome: Outcome = "created" if created else "joined" if joined else "updated"
        return Mined(obs.trace_id, outcome, gid, why)

    # ------------------------------------------------------------ fixes

    async def record_fixes(
        self, conn: Conn, run: Mapping[str, Any], cases: Sequence[Mapping[str, Any]]
    ) -> list[str]:
        """Fixes the promoted groups whose scenario the run's candidate passed
        (inside the run's completing transaction). Returns their ids."""
        passed = sorted(
            {
                str(c["scenario_name"])
                for c in cases
                if ((c.get("comparison") or {}).get("candidate") or {}).get("status") == "PASSED"
            }
        )
        fixed: list[str] = []
        version = str(run["candidate_version"])
        groups = await self.store.promoted_for(
            conn, str(run["organization_id"]), str(run["project_id"]), str(run["agent_name"]), passed
        )
        for g in groups:
            to = transition(str(g["status"]), "fixed", has_case=g["scenario_name"] is not None)
            await self.store.set_status(
                conn, str(g["id"]), to, {"fixed_version": version, "fixed_eval_run_id": str(run["id"])}
            )
            await self.store.add_event(
                conn,
                str(g["id"]),
                action="fixed",
                actor=EVALUATION_ACTOR,
                from_status=str(g["status"]),
                to_status=to,
                reason=f"{version} passed {g['scenario_name']} in evaluation run {run['id']}",
                detail={"eval_run_id": str(run["id"]), "agent_version": version},
            )
            fixed.append(str(g["id"]))
        return fixed


def _observation_json(obs: Observation) -> dict[str, Any]:
    """The observation as stored: structured fields only (no content)."""
    return {k: list(v) if isinstance(v, tuple) else v for k, v in asdict(obs).items()}
