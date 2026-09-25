"""Helpers of the regression tests: the recorded demo traces
(``data/regressions``), variants of them, and the trace events the trace
service writes for them."""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from typing import Any

from agenttwin_core.events import Envelope, validate_envelope
from agenttwin_evaluation.miner import Mined
from eval_testutil import ORG, PROJECT, Stack

DATA = Path(__file__).parent / "data" / "regressions"
AGENT = "support-refund-agent"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((DATA / f"{name}.json").read_text())
    return data


def trace_id() -> str:
    return uuid.uuid4().hex


def variant(detail: dict[str, Any], **trace: Any) -> dict[str, Any]:
    """A copy of a trace detail, as another trace: a new id unless given,
    and the trace fields given (``summary`` fields are merged)."""
    d = copy.deepcopy(detail)
    summary = trace.pop("summary", {})
    d["trace"].update({"trace_id": trace_id()} | trace)
    d["trace"]["summary"].update(summary)
    return d


def ingested(detail: dict[str, Any], project: str = PROJECT) -> Envelope:
    """``trace.ingested.v1`` for the trace, as the trace service writes it."""
    t = detail["trace"]
    counts: dict[str, int] = {}
    risks: dict[str, str] = {}
    for s in detail["spans"]:
        if s.get("kind") == "tool":
            counts[s["tool_name"]] = counts.get(s["tool_name"], 0) + 1
            risks[s["tool_name"]] = s["tool_risk"]
    payload = {
        "trace_id": t["trace_id"],
        "agent": t["agent_name"],
        "agent_version": t["agent_version"],
        "environment": t["environment"],
        "source": t["source"],
        "started_at": t["started_at"],
        "signals": t["signals"],
        "summary": t["summary"],
        "observed_tools": [
            {"name": n, "risk": risks[n], "http_host": None, "count": counts[n]} for n in sorted(counts)
        ],
        "simulation_run_id": t["simulation_run_id"],
    }
    env = Envelope.new("trace.ingested.v1", "trace-service", ORG, project, t["trace_id"], payload)
    return validate_envelope(env.to_dict())


def outcome_recorded(detail: dict[str, Any], project: str = PROJECT) -> Envelope:
    t = detail["trace"]
    payload = {
        "trace_id": t["trace_id"],
        "status": t["outcome_status"] or "UNKNOWN",
        "verified": bool(t["outcome_verified"]),
        "verification_source": "state_assertion",
        "contradiction": "contradiction" in t["signals"],
        "source": "api",
    }
    env = Envelope.new("trace.outcome_recorded.v1", "trace-service", ORG, project, None, payload)
    return validate_envelope(env.to_dict())


def flagged(tid: str, reason: str, kind: str = "incident", project: str = PROJECT) -> Envelope:
    payload = {"trace_id": tid, "reason": reason, "kind": kind, "flagged_by": "user:support"}
    env = Envelope.new("trace.flagged.v1", "trace-service", ORG, project, None, payload)
    return validate_envelope(env.to_dict())


def clean(detail: dict[str, Any]) -> dict[str, Any]:
    """The trace without any failure signal: a verified success."""
    d = variant(
        detail,
        signals=[],
        outcome_status="SUCCESS",
        outcome_verified=True,
        error_count=0,
        summary={
            "outcome": "SUCCESS",
            "outcome_verified": True,
            "errors": [],
            "violations": [],
            "retry_count": 0,
        },
    )
    for key in ("failing_tool", "error_type"):
        d["trace"]["summary"].pop(key, None)
    return d


async def mine(ev: Stack, env: Envelope) -> Mined:
    assert ev.worker.miner is not None
    mined = await ev.worker.miner.on_event(env)
    assert mined is not None
    return mined


async def mined(ev: Stack, name: str, *, project: str = PROJECT, **trace: Any) -> tuple[str, dict[str, Any]]:
    """Mines the recorded trace (a variant of it when trace fields are
    given) and serves its detail from the trace service; returns its group."""
    detail = variant(load(name), **trace) if trace else load(name)
    ev.traces.details[detail["trace"]["trace_id"]] = detail
    result = await mine(ev, ingested(detail, project))
    assert result.group_id is not None
    return result.group_id, detail
