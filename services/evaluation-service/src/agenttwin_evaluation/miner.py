"""The regression miner at work (ADR-0032): trace events in, groups out.

``trace.ingested.v1`` carries a finalized trace's summary and signals;
``trace.outcome_recorded.v1`` and ``trace.flagged.v1`` say something new
about a trace, so its detail is read again from the trace service (the
authority on it). ``policy.violation_detected.v1`` (ADR-0033) says the
runtime gateway refused one of a trace's actions: a denial is a failure
signal even when the agent did not record the decision itself (an approval
request is not). Each event is applied once (``processed_event``), in one
transaction:

1. the observation of the trace (flags and runtime denials received before it
   was finalized are kept in ``regression_flag`` and
   ``regression_runtime_denial`` and added);
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

import re
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
    with_runtime_denial,
)
from agenttwin_evaluation.regression_store import MINER_ACTOR, RegressionStore

__all__ = ["CONSUMER", "EVALUATION_ACTOR", "MINED_EVENTS", "RUNTIME_EVENTS", "TRACE_EVENTS", "Mined", "Miner"]

CONSUMER = "evaluation-service.regression-miner"
EVALUATION_ACTOR = "system:evaluation"
TRACE_EVENTS = frozenset({"trace.ingested.v1", "trace.outcome_recorded.v1", "trace.flagged.v1"})
RUNTIME_EVENTS = frozenset({"policy.violation_detected.v1"})
#: The events the miner applies.
MINED_EVENTS = TRACE_EVENTS | RUNTIME_EVENTS
_TRACE_ID = re.compile(r"[a-f0-9]{32}")

Outcome = Literal[
    "ignored",  # not a production trace, no longer known to the trace service, or not a failure
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


class _NeedsCurrent(Exception):
    """A snapshot of a trace cannot decide this mining: read the trace again."""


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
        """Applies a trace event or a runtime denial (other events: None)."""
        if env.type in RUNTIME_EVENTS:
            return await self._on_runtime(env)
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
        # An ingestion event carries a snapshot of the trace. Events arrive out
        # of order: an outcome recorded later (or a flag, a denial) may already
        # have mined the trace as it is now, and the older snapshot must not
        # undo that. So a snapshot is only trusted to say "nothing to do"; a
        # mining that would create or change an occurrence reads the trace
        # again (it is finalized once its ingestion is announced, and every
        # other event mines only a finalized trace, so what is read is never
        # older than the snapshot).
        try:
            mined = await self._apply(
                env, org, project, trace_id, obs, flag, finalized, snapshot=env.type == "trace.ingested.v1"
            )
        except _NeedsCurrent:
            current = await self.traces.trace(org, project, trace_id)
            if current is not None:  # else the trace is gone: the snapshot is all there is
                obs = observation_from_detail(current)
            mined = await self._apply(env, org, project, trace_id, obs, flag, finalized, snapshot=False)
        if mined.outcome in ("duplicate", "waiting"):
            return mined
        self.log.info(
            "trace mined",
            trace_id=trace_id,
            outcome=mined.outcome,
            regression_group_id=mined.group_id,
            event=env.type,
        )
        return mined

    async def _apply(
        self,
        env: Envelope,
        org: str,
        project: str,
        trace_id: str,
        obs: Observation,
        flag: tuple[str, str, str | None] | None,
        finalized: bool,
        *,
        snapshot: bool,
    ) -> Mined:
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
            obs = await self._known(conn, project, obs)
            return await self.mine(conn, org, project, obs, occurred_at=env.occurred_at, snapshot=snapshot)

    async def _known(self, conn: Conn, project: str, obs: Observation) -> Observation:
        """The observation with what was received about the trace before:
        people's flags and the runtime gateway's denials."""
        for kind, reason in await self.store.flags_of(conn, project, obs.trace_id):
            obs = with_flag(obs, kind, reason)
        for tool in await self.store.runtime_denials_of(conn, project, obs.trace_id):
            obs = with_runtime_denial(obs, tool)
        return obs

    async def _on_runtime(self, env: Envelope) -> Mined:
        """``policy.violation_detected.v1``: the runtime gateway refused an
        action of the trace (a policy denied it, or its approval token did
        not fit it), or held it for approval. A refusal is kept for the
        trace and the trace is mined again; an approval request is not a
        failure."""
        project, payload = env.project_id, env.payload
        if not project:
            raise Permanent(f"{env.type} names no project")
        trace_id = str(payload.get("trace_id") or "").strip().lower()
        tool = str(payload.get("tool") or "").strip()
        refused = payload.get("decision") == "deny" or payload.get("outcome") == "approval_refused"
        if not refused:
            return Mined(trace_id, "ignored", reason="an approval request is not a failure")
        if not trace_id:
            return Mined(trace_id, "ignored", reason="the call named no trace")
        decision_id = str(payload.get("decision_id") or "")
        if not tool or not decision_id or not _TRACE_ID.fullmatch(trace_id):
            raise Permanent(f"{env.type} names no tool, no decision or no valid trace")
        environment = payload.get("environment")
        if environment not in (None, "", "production"):
            return Mined(trace_id, "ignored", reason=f"a {environment} call, not a production one")
        org = env.organization_id
        detail = await self.traces.trace(org, project, trace_id)
        obs = observation_from_detail(detail) if detail is not None else None
        if obs is not None and not obs.production:
            return Mined(trace_id, "ignored")
        async with transaction(self.pool) as conn:
            if not await claim_event(conn, CONSUMER, env.id):
                return Mined(trace_id, "duplicate")
            await self.store.add_runtime_denial(
                conn,
                decision_id=decision_id,
                project_id=project,
                trace_id=trace_id,
                tool=tool,
                rule=str(payload.get("rule") or ""),
                outcome=str(payload["outcome"]) if payload.get("outcome") else None,
                reason=str(payload.get("reason") or "").strip()[:2000] or None,
            )
            if obs is None or not (detail and detail["trace"].get("finalized")):
                # Its ingestion will mine it, with this denial.
                return Mined(trace_id, "waiting", reason="the trace is not finalized yet")
            obs = await self._known(conn, project, obs)
            mined = await self.mine(conn, org, project, obs, occurred_at=env.occurred_at)
        self.log.info(
            "trace mined",
            trace_id=trace_id,
            outcome=mined.outcome,
            regression_group_id=mined.group_id,
            event=env.type,
            tool=tool,
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

    async def mine(
        self,
        conn: Conn,
        org: str,
        project: str,
        obs: Observation,
        *,
        occurred_at: str,
        snapshot: bool = False,
    ) -> Mined:
        """Stores (or withdraws) the trace's occurrence and groups it. From a
        ``snapshot``, only "nothing to do" is decided; anything else raises
        _NeedsCurrent (the transaction is rolled back and the caller reads
        the trace again)."""
        reasons = detect(obs)
        await self.store.lock_agent(conn, project, obs.agent)
        existing = await self.store.occurrence(conn, project, obs.trace_id)
        if snapshot and (reasons or existing is not None):
            raise _NeedsCurrent
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
