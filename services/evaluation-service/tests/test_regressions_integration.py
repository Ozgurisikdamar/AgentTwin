"""The regression miner on PostgreSQL (ADR-0032): trace events of real demo
traces in, groups out — known failures, similar ones, new ones; outcomes and
flags that arrive later; tenancy; fixes by an evaluation run and reopening.
Every event is checked against its published schema and every call to the
trace service against its contract."""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any

import pytest

from agenttwin_core.db import transaction
from agenttwin_core.embeddings import EmbeddingError
from agenttwin_core.events import Envelope, Permanent
from agenttwin_core.ids import new_id
from agenttwin_evaluation.clients import UpstreamError
from agenttwin_evaluation.miner import CONSUMER, EVALUATION_ACTOR, Miner
from agenttwin_evaluation.mining import detect, observation_from_event, suggest_taxonomy
from agenttwin_evaluation.regression_store import MINER_ACTOR
from eval_testutil import (
    ORG,
    OTHER_PROJECT,
    PROJECT,
    Stack,
    evaluation_stack,
    evaluation_with_simulation,
    run_simulations,
)
from regression_testutil import (
    AGENT,
    clean,
    flagged,
    ingested,
    load,
    mine,
    outcome_recorded,
    variant,
    violation,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def groups(ev: Stack, project: str = PROJECT) -> list[dict[str, Any]]:
    return await ev.store.all(
        "SELECT * FROM regression_group WHERE project_id = %s ORDER BY created_at, id", (project,)
    )


async def occurrences(ev: Stack, group_id: Any) -> list[dict[str, Any]]:
    return await ev.store.all(
        """SELECT *, embedding IS NOT NULL AS embedded FROM regression_occurrence
           WHERE group_id = %s ORDER BY started_at, trace_id""",
        (group_id,),
    )


async def occurrence(ev: Stack, tid: str) -> dict[str, Any]:
    row = await ev.store.one("SELECT * FROM regression_occurrence WHERE trace_id = %s", (tid,))
    assert row is not None
    return row


async def events(ev: Stack, group_id: Any) -> list[tuple[Any, ...]]:
    rows = await ev.regressions.events(str(group_id))
    return [(r["action"], r["from_status"], r["to_status"], r["actor"]) for r in rows]


async def promote(ev: Stack, group_id: Any, scenario: str) -> None:
    """What the promotion API (P6c) leaves behind: the group linked to its
    scenario, PROMOTED."""
    async with transaction(ev.pool) as conn:
        await conn.execute(
            """UPDATE regression_group SET status = 'PROMOTED', scenario_name = %s, scenario_id = %s,
                      promoted_by = 'user:reviewer', promoted_at = now() WHERE id = %s""",
            (scenario, new_id(), group_id),
        )
        await ev.regressions.add_event(
            conn,
            str(group_id),
            action="promote",
            actor="user:reviewer",
            from_status="CANDIDATE",
            to_status="PROMOTED",
        )


# ---------------------------------------------------------------- grouping


async def test_a_production_failure_becomes_a_group_and_a_repeat_joins_it() -> None:
    async with evaluation_stack() as ev:
        first = load("duplicate-refund")
        env = ingested(first)
        # Through the worker's event handler (the queue's dispatch).
        await ev.worker.on_event(env)
        [g] = await groups(ev)
        tid = first["trace"]["trace_id"]
        obs = observation_from_event(env.payload)
        s = suggest_taxonomy(obs)
        assert (
            g["status"],
            g["taxonomy"],
            g["suggested_taxonomy"],
            g["severity"],
            g["suggested_severity"],
        ) == (
            "CANDIDATE",
            "DUPLICATE_SIDE_EFFECT",
            "DUPLICATE_SIDE_EFFECT",
            "critical",
            "critical",
        )
        assert g["title"] == "refund_payment took effect twice"
        assert g["component"] == "refund_payment"
        assert g["secondary"] == ["HALLUCINATED_SUCCESS", "RETRY_SAFETY", "TIMEOUT"]
        assert g["evidence"] == list(s.evidence)
        assert g["severity_reason"]
        assert (g["occurrence_count"], g["versions"], g["environments"]) == (1, ["1.3.0"], ["production"])
        assert g["representative_trace_id"] == tid and g["first_seen"] == g["last_seen"]
        assert g["first_seen"] == datetime.fromisoformat(first["trace"]["started_at"])
        assert (g["agent_name"], str(g["organization_id"]), g["merged_into"], g["triaged_by"]) == (
            AGENT,
            ORG,
            None,
            None,
        )
        [o] = await occurrences(ev, g["id"])
        assert o["reasons"] == detect(obs) and o["join_kind"] == "new"
        assert o["join_reason"] == "the first failure of its kind"
        assert (o["embedded"], o["model"], o["dims"]) == (True, "hashing-v1", 256)
        # Features only: the conversation is never stored.
        text = json.dumps([o["observation"], o["features"]])
        assert "ORD-" not in text and "refund of $40" not in text
        assert await events(ev, g["id"]) == [("created", None, "CANDIDATE", MINER_ACTOR)]

        # Redelivered: applied once.
        assert (await mine(ev, env)).outcome == "duplicate"
        assert [r["occurrence_count"] for r in await groups(ev)] == [1]

        # The same failure in a later version, a minute later: the known failure.
        later = variant(first, agent_version="1.3.1", started_at="2026-09-25T16:00:54.000000Z")
        mined = await mine(ev, ingested(later))
        assert (mined.outcome, mined.group_id) == ("joined", str(g["id"]))
        [g2] = await groups(ev)
        assert (g2["occurrence_count"], g2["versions"]) == (2, ["1.3.0", "1.3.1"])
        assert g2["first_seen"] == g["first_seen"] and g2["last_seen"] > g["last_seen"]
        assert g2["representative_trace_id"] == tid
        assert [o["join_kind"] for o in await occurrences(ev, g["id"])] == ["new", "exact"]
        assert len(await events(ev, g["id"])) == 1
        processed = await ev.store.all(
            "SELECT event_id FROM processed_event WHERE consumer = %s", (CONSUMER,)
        )
        assert len(processed) == 2


async def test_a_similar_failure_joins_and_a_different_one_starts_its_own_group() -> None:
    async with evaluation_stack() as ev:
        dup = load("duplicate-refund")
        # A farther failure first (0.56 from the one below): the nearest wins.
        denied = load("cross-tenant-denied")
        await mine(ev, ingested(denied))
        await mine(ev, ingested(dup))
        # The same duplicate refund, after an upstream error instead of a
        # timeout: another fingerprint, close features (0.91).
        other_error = variant(
            dup,
            signals=[s for s in dup["trace"]["signals"] if s != "timeout_after_mutation"],
            summary={"error_type": "upstream_5xx", "errors": ["refund_payment:upstream_5xx"]},
        )
        mined = await mine(ev, ingested(other_error))
        [_, g] = await groups(ev)
        assert (mined.outcome, mined.group_id) == ("joined", str(g["id"]))
        o = await occurrence(ev, other_error["trace"]["trace_id"])
        assert o["join_kind"] == "similar" and 0.9 < o["similarity"] < 0.92
        assert o["join_reason"].startswith("close to a known failure (similarity 0.91")
        # Its fingerprint now names the group: the next one like it is known.
        again = variant(other_error)
        assert (await mine(ev, ingested(again))).group_id == str(g["id"])
        assert (await occurrence(ev, again["trace"]["trace_id"]))["join_kind"] == "exact"

        # A denied cross-tenant read is another failure (0.56): a group of one.
        g2 = (await groups(ev))[0]
        assert (g2["taxonomy"], g2["title"], g2["occurrence_count"]) == (
            "AUTHORIZATION",
            "Access to lookup_order was denied",
            1,
        )
        [o2] = await occurrences(ev, g2["id"])
        assert o2["join_kind"] == "new" and o2["join_reason"] == "the first failure of its kind"
        o1 = await occurrence(ev, dup["trace"]["trace_id"])
        assert o1["join_kind"] == "new" and o1["join_reason"].startswith("no known failure is close enough")


async def test_groups_and_neighbours_never_cross_projects() -> None:
    async with evaluation_stack() as ev:
        dup = load("duplicate-refund")
        await mine(ev, ingested(dup))
        # The same failure in another project of the organization: its own group.
        mined = await mine(ev, ingested(variant(dup), project=OTHER_PROJECT))
        [mine_g] = await groups(ev)
        [other_g] = await groups(ev, OTHER_PROJECT)
        assert mined.outcome == "created" and mined.group_id == str(other_g["id"]) != str(mine_g["id"])
        assert mine_g["occurrence_count"] == other_g["occurrence_count"] == 1
        # A close failure in the other project joins the other project's group.
        close = variant(
            dup, summary={"error_type": "upstream_5xx", "errors": ["refund_payment:upstream_5xx"]}
        )
        assert (await mine(ev, ingested(close, project=OTHER_PROJECT))).group_id == str(other_g["id"])
        assert [g["occurrence_count"] for g in await groups(ev)] == [1]


async def test_failures_of_a_new_kind_arriving_together_make_one_group() -> None:
    async with evaluation_stack() as ev:
        dup = load("duplicate-refund")
        # Three bursts of eight: without serialization two of a burst would
        # each find no group and make one.
        for n, kind in enumerate((dup, load("cross-tenant-denied"), load("timeout-handled")), 1):
            results = await asyncio.gather(*(mine(ev, ingested(variant(kind))) for _ in range(8)))
            assert sorted(r.outcome for r in results) == ["created"] + ["joined"] * 7
            assert len(await groups(ev)) == n
            assert len({r.group_id for r in results}) == 1
        assert [g["occurrence_count"] for g in await groups(ev)] == [8, 8, 8]


class _Embedder:
    """An embedder that answers zeros, or fails."""

    model = "test-embedder"
    dims = 16

    def __init__(self, error: EmbeddingError | None = None) -> None:
        self.error = error

    async def embed(self, texts: Any) -> list[list[float]]:
        if self.error is not None:
            raise self.error
        return [[0.0] * self.dims for _ in texts]


def miner_with(ev: Stack, embedder: _Embedder) -> Miner:
    assert ev.worker.miner is not None
    return replace(ev.worker.miner, embedder=embedder)


async def test_features_without_a_vector_are_grouped_by_fingerprint_only() -> None:
    async with evaluation_stack() as ev:
        zero = miner_with(ev, _Embedder())
        dup = load("duplicate-refund")
        assert (await zero.on_event(ingested(dup))).outcome == "created"  # type: ignore[union-attr]
        # A close failure cannot be compared: a group of its own.
        close = variant(
            dup, summary={"error_type": "upstream_5xx", "errors": ["refund_payment:upstream_5xx"]}
        )
        assert (await zero.on_event(ingested(close))).outcome == "created"  # type: ignore[union-attr]
        rows = await ev.store.all(
            "SELECT embedding IS NULL AS empty, model, dims, join_reason FROM regression_occurrence"
        )
        assert {(r["empty"], r["model"], r["dims"], r["join_reason"]) for r in rows} == {
            (True, "test-embedder", 16, "the first failure of its kind")
        }
        # The same failure is still known.
        again = await zero.on_event(ingested(variant(close)))
        assert again is not None and again.outcome == "joined"


async def test_an_embedder_that_fails_retries_or_goes_without() -> None:
    async with evaluation_stack() as ev:
        busy = miner_with(ev, _Embedder(EmbeddingError("rate_limited", "busy", retryable=True)))
        env = ingested(load("duplicate-refund"))
        with pytest.raises(EmbeddingError):
            await busy.on_event(env)
        assert await ev.store.all("SELECT * FROM processed_event") == []
        refused = miner_with(ev, _Embedder(EmbeddingError("rejected", "no")))
        mined = await refused.on_event(env)
        assert mined is not None and mined.outcome == "created"
        [row] = await ev.store.all("SELECT embedding IS NULL AS empty FROM regression_occurrence")
        assert row["empty"] is True


async def test_other_events_are_not_the_miners() -> None:
    async with evaluation_stack() as ev:
        assert ev.worker.miner is not None
        env = Envelope.new(
            "simulation.run_completed.v1",
            "simulation-service",
            ORG,
            PROJECT,
            None,
            {"run_id": str(uuid.uuid4()), "status": "COMPLETED"},
        )
        assert await ev.worker.miner.on_event(env) is None


# ---------------------------------------------------------------- what is not mined


async def test_simulations_and_successes_are_not_mined() -> None:
    async with evaluation_stack() as ev:
        dup = load("duplicate-refund")
        simulated = variant(dup, source="simulation", simulation_run_id=str(uuid.uuid4()))
        assert (await mine(ev, ingested(simulated))).outcome == "ignored"
        assert await ev.store.all("SELECT * FROM processed_event") == []
        assert (await mine(ev, ingested(clean(dup)))).outcome == "not_candidate"
        assert await groups(ev) == []
        # An unverified outcome alone is not a failure.
        unverified = variant(clean(dup), outcome_verified=False, summary={"outcome_verified": False})
        assert (await mine(ev, ingested(unverified))).outcome == "not_candidate"


# ---------------------------------------------------------------- later facts


async def test_an_outcome_recorded_later_makes_or_unmakes_a_candidate() -> None:
    async with evaluation_stack() as ev:
        ok = clean(load("duplicate-refund"))
        tid = ok["trace"]["trace_id"]
        assert (await mine(ev, ingested(ok))).outcome == "not_candidate"

        # The verified outcome says the refund went wrong: the trace service
        # is read again, and the trace becomes a candidate.
        failed = copy.deepcopy(ok)
        failed["trace"].update(outcome_status="FAILURE", signals=["contradiction", "outcome_failure"])
        failed["trace"]["summary"].update(outcome="FAILURE")
        ev.traces.details[tid] = failed
        mined = await mine(ev, outcome_recorded(failed))
        assert mined.outcome == "created"
        [g] = await groups(ev)
        assert (g["taxonomy"], g["severity"]) == ("HALLUCINATED_SUCCESS", "critical")
        assert ev.traces.calls[-1]["project_id"] == PROJECT

        # The same failure again, a minute later.
        other = variant(failed, started_at="2026-09-25T16:00:54.000000Z")
        assert (await mine(ev, ingested(other))).group_id == str(g["id"])
        # The first outcome is corrected: no longer a failure. The group keeps
        # the other trace, which now represents it.
        ev.traces.details[tid] = ok
        mined = await mine(ev, outcome_recorded(ok))
        assert (mined.outcome, mined.group_id) == ("withdrawn", str(g["id"]))
        [kept] = await groups(ev)
        assert (kept["occurrence_count"], kept["representative_trace_id"]) == (1, other["trace"]["trace_id"])
        # And once that one is corrected too, the group nobody looked at goes.
        ev.traces.details[other["trace"]["trace_id"]] = clean(other) | {
            "trace": clean(other)["trace"] | {"trace_id": other["trace"]["trace_id"]}
        }
        fixed_other = ev.traces.details[other["trace"]["trace_id"]]
        assert (await mine(ev, outcome_recorded(fixed_other))).outcome == "withdrawn"
        assert await groups(ev) == []
        assert await ev.store.all("SELECT * FROM regression_occurrence") == []


async def test_a_failure_that_changes_kind_moves_to_its_new_group() -> None:
    async with evaluation_stack() as ev:

        def lied(detail: dict[str, Any]) -> dict[str, Any]:
            """The success it reported was false."""
            d = copy.deepcopy(detail)
            d["trace"].update(
                outcome_status="FAILURE", signals=[*detail["trace"]["signals"], "contradiction"]
            )
            d["trace"]["summary"].update(outcome="FAILURE")
            ev.traces.details[d["trace"]["trace_id"]] = d
            return d

        first = load("timeout-handled")
        await mine(ev, ingested(first))
        [timeout] = await groups(ev)
        assert timeout["taxonomy"] == "TIMEOUT"
        # Now a hallucinated success: the trace moves to a group of its new
        # kind, and the group it left, which nobody looked at, goes.
        assert (await mine(ev, outcome_recorded(lied(first)))).outcome == "created"
        [hallucinated] = await groups(ev)
        assert (hallucinated["taxonomy"], hallucinated["occurrence_count"]) == ("HALLUCINATED_SUCCESS", 1)
        assert hallucinated["id"] != timeout["id"]

        # Updating what is known without a change of kind keeps it in place.
        again = await mine(ev, outcome_recorded(lied(first)))
        assert (again.outcome, again.group_id) == ("updated", str(hallucinated["id"]))
        kept = await occurrence(ev, first["trace"]["trace_id"])
        assert (kept["join_kind"], kept["join_reason"]) == ("new", "the first failure of its kind")

        # A group someone was assigned to, or acted on, stays when its last
        # trace leaves it.
        async def emptied(name: str, act: str) -> dict[str, Any]:
            detail = variant(load(name))
            tid = detail["trace"]["trace_id"]
            gid = (await mine(ev, ingested(detail))).group_id
            async with transaction(ev.pool) as conn:
                if act == "assignee":
                    await conn.execute(
                        "UPDATE regression_group SET assignee = 'user:lead' WHERE id = %s", (gid,)
                    )
                else:
                    await ev.regressions.add_event(conn, str(gid), action="triage", actor="user:lead")
            corrected = clean(detail)
            corrected["trace"]["trace_id"] = tid
            ev.traces.details[tid] = corrected
            mined = await mine(ev, outcome_recorded(corrected))
            assert (mined.outcome, mined.group_id) == ("withdrawn", gid)
            got = await ev.store.one("SELECT * FROM regression_group WHERE id = %s", (gid,))
            assert got is not None and got["occurrence_count"] == 0
            return got

        await emptied("cross-tenant-denied", "assignee")
        await emptied("duplicate-refund", "event")
        counts = {g["taxonomy"]: g["occurrence_count"] for g in await groups(ev)}
        assert counts == {"HALLUCINATED_SUCCESS": 1, "AUTHORIZATION": 0, "DUPLICATE_SIDE_EFFECT": 0}


async def test_a_group_is_derived_from_its_occurrences() -> None:
    async with evaluation_stack() as ev:
        # A handled timeout (medium) at 16:00.
        first = variant(load("timeout-handled"), started_at="2026-09-25T16:00:00.000000Z")
        await mine(ev, ingested(first))
        [g] = await groups(ev)
        assert (g["severity"], g["representative_trace_id"]) == ("medium", first["trace"]["trace_id"])

        # The same failure, earlier (15:00), of no known version, flagged as an
        # incident before it was finalized (high).
        second = variant(first, agent_version=None, started_at="2026-09-25T15:00:00.000000Z")
        second["trace"]["summary"].pop("agent_version")
        sid = second["trace"]["trace_id"]
        ev.traces.details[sid] = copy.deepcopy(second)
        ev.traces.details[sid]["trace"]["finalized"] = False
        await mine(ev, flagged(sid, "The customer called"))
        assert (await mine(ev, ingested(second))).group_id == str(g["id"])
        [g] = await groups(ev)
        # The worst severity, the whole time span, the known versions; the
        # first trace still represents it.
        assert (g["severity"], g["suggested_severity"], g["severity_reason"]) == (
            "high",
            "high",
            "flagged as an incident",
        )
        assert (g["first_seen"].hour, g["last_seen"].hour, g["versions"]) == (15, 16, ["1.2.4"])
        assert (g["representative_trace_id"], g["occurrence_count"]) == (first["trace"]["trace_id"], 2)

        # A person sets the label and severity: new failures do not change them.
        async with transaction(ev.pool) as conn:
            await conn.execute(
                """UPDATE regression_group SET triaged_by = 'user:lead', severity = 'low',
                          taxonomy = 'TOOL_ERROR_HANDLING', secondary = '{}' WHERE id = %s""",
                (g["id"],),
            )
        third = variant(first, started_at="2026-09-25T17:00:00.000000Z")
        ev.traces.details[third["trace"]["trace_id"]] = third | {
            "flags": [
                {
                    "id": new_id(),
                    "kind": "incident",
                    "reason": "Again",
                    "flagged_by": "user:support",
                    "created_at": "2026-09-25T17:05:00Z",
                }
            ]
        }
        await mine(ev, ingested(third))
        await mine(ev, flagged(third["trace"]["trace_id"], "Again"))
        [g] = await groups(ev)
        assert (g["severity"], g["taxonomy"], g["secondary"], g["suggested_severity"]) == (
            "low",
            "TOOL_ERROR_HANDLING",
            [],
            "high",
        )
        assert g["suggested_taxonomy"] == "TIMEOUT" and g["occurrence_count"] == 3

        # The representative leaves (its outcome was a false alarm): the worst,
        # earliest remaining trace represents the group.
        corrected = clean(first)
        corrected["trace"]["trace_id"] = first["trace"]["trace_id"]
        ev.traces.details[first["trace"]["trace_id"]] = corrected
        await mine(ev, outcome_recorded(corrected))
        [g] = await groups(ev)
        assert (g["representative_trace_id"], g["occurrence_count"], g["first_seen"].hour) == (sid, 2, 15)


async def test_a_flag_is_kept_until_its_trace_is_finalized() -> None:
    async with evaluation_stack() as ev:
        ok = clean(load("duplicate-refund"))
        tid = ok["trace"]["trace_id"]
        pending = copy.deepcopy(ok)
        pending["trace"]["finalized"] = False
        ev.traces.details[tid] = pending
        flag = flagged(tid, "Customer says the refund never arrived")
        assert (await mine(ev, flag)).outcome == "waiting"
        assert (await mine(ev, flag)).outcome == "duplicate"
        rows = await ev.store.all(
            "SELECT kind, reason, flagged_by FROM regression_flag WHERE trace_id = %s", (tid,)
        )
        assert rows == [
            {
                "kind": "incident",
                "reason": "Customer says the refund never arrived",
                "flagged_by": "user:support",
            }
        ]
        # Finalized: its ingestion carries no failure, the flag makes it one.
        mined = await mine(ev, ingested(ok))
        assert mined.outcome == "created"
        [g] = await groups(ev)
        [o] = await occurrences(ev, g["id"])
        assert o["reasons"] == ["flagged as incident: Customer says the refund never arrived"]
        assert (g["severity"], g["severity_reason"]) == ("high", "flagged as an incident")
        assert o["observation"]["flags"] == [["incident", "Customer says the refund never arrived"]]


async def test_a_flag_on_a_finalized_trace_reads_it_again() -> None:
    async with evaluation_stack() as ev:
        ok = clean(load("duplicate-refund"))
        tid = ok["trace"]["trace_id"]
        await mine(ev, ingested(ok))
        flag = {
            "id": new_id(),
            "kind": "negative_feedback",
            "reason": "Rude reply",
            "flagged_by": "user:support",
            "created_at": "2026-09-25T16:10:00Z",
        }
        with_flag = copy.deepcopy(ok) | {"flags": [flag]}
        ev.traces.details[tid] = with_flag
        mined = await mine(ev, flagged(tid, "Rude reply", "negative_feedback"))
        assert mined.outcome == "created"
        [g] = await groups(ev)
        [o] = await occurrences(ev, g["id"])
        # Once, though both the trace service and the event name it.
        assert o["reasons"] == ["flagged as negative feedback: Rude reply"]
        assert g["severity"] == "medium"


async def test_an_unreachable_trace_service_leaves_the_event_for_a_retry() -> None:
    async with evaluation_stack() as ev:
        dup = load("duplicate-refund")
        tid = dup["trace"]["trace_id"]
        ev.traces.details[tid] = dup
        ev.traces.fail = 503
        with pytest.raises(UpstreamError):
            await mine(ev, outcome_recorded(dup))
        assert await ev.store.all("SELECT * FROM processed_event") == []
        ev.traces.fail = None
        assert (await mine(ev, outcome_recorded(dup))).outcome == "created"
        # A trace the trace service no longer knows (retention) is let go.
        gone = variant(dup)
        assert (await mine(ev, outcome_recorded(gone))).outcome == "ignored"


async def test_a_merged_group_sends_new_failures_to_the_one_it_joined() -> None:
    async with evaluation_stack() as ev:
        await mine(ev, ingested(load("duplicate-refund")))
        await mine(ev, ingested(load("cross-tenant-denied")))
        dup, denied = await groups(ev)
        async with transaction(ev.pool) as conn:
            await conn.execute(
                "UPDATE regression_group SET merged_into = %s WHERE id = %s", (dup["id"], denied["id"])
            )
        mined = await mine(ev, ingested(variant(load("cross-tenant-denied"))))
        assert mined.group_id == str(dup["id"])
        assert [g["occurrence_count"] for g in await groups(ev)] == [2, 1]


# ---------------------------------------------------------------- runtime denials


async def denials(ev: Stack, tid: str) -> list[tuple[Any, ...]]:
    rows = await ev.store.all(
        "SELECT tool, rule, outcome, reason FROM regression_runtime_denial WHERE trace_id = %s", (tid,)
    )
    return [(r["tool"], r["rule"], r["outcome"], r["reason"]) for r in rows]


async def test_a_runtime_denial_makes_a_trace_a_policy_violation() -> None:
    async with evaluation_stack() as ev:
        ok = clean(load("duplicate-refund"))
        tid = ok["trace"]["trace_id"]
        ev.traces.details[tid] = ok
        assert (await mine(ev, ingested(ok))).outcome == "not_candidate"
        # The agent recorded nothing; the gateway's event is enough.
        env = violation(tid)
        mined = await mine(ev, env)
        assert mined.outcome == "created"
        assert (await mine(ev, env)).outcome == "duplicate"
        [g] = await groups(ev)
        [o] = await occurrences(ev, g["id"])
        assert o["reasons"] == ["a policy denied one of its actions"]
        assert (g["taxonomy"], g["severity"]) == ("POLICY_VIOLATION", "high")
        assert o["observation"]["violations"] == ["policy_denied:refund_payment"]
        assert await denials(ev, tid) == [
            ("refund_payment", "refund_over_limit", "denied", "Refunds over 100 USD need a person's approval.")
        ]
        # A later fact about the trace reads it again and keeps the denial.
        assert (await mine(ev, outcome_recorded(ok))).outcome == "updated"
        [o] = await occurrences(ev, g["id"])
        assert o["observation"]["violations"] == ["policy_denied:refund_payment"]


async def test_a_runtime_denial_before_its_trace_waits_for_the_ingestion() -> None:
    async with evaluation_stack() as ev:
        ok = clean(load("duplicate-refund"))
        tid = ok["trace"]["trace_id"]
        # The trace service does not know the trace yet.
        assert (await mine(ev, violation(tid, tool="issue_credit"))).outcome == "waiting"
        assert await groups(ev) == []
        mined = await mine(ev, ingested(ok))
        assert mined.outcome == "created"
        [o] = await occurrences(ev, mined.group_id)
        assert o["observation"]["violations"] == ["policy_denied:issue_credit"]
        # Known but not finalized: kept too.
        other = clean(load("duplicate-refund"))
        pending = copy.deepcopy(other)
        pending["trace"]["finalized"] = False
        ev.traces.details[other["trace"]["trace_id"]] = pending
        assert (await mine(ev, violation(other["trace"]["trace_id"]))).outcome == "waiting"
        assert len(await denials(ev, other["trace"]["trace_id"])) == 1


async def test_a_refused_approval_token_is_a_denial_and_an_approval_request_is_not() -> None:
    async with evaluation_stack() as ev:
        ok = clean(load("duplicate-refund"))
        tid = ok["trace"]["trace_id"]
        ev.traces.details[tid] = ok
        asked = await mine(ev, violation(tid, decision="require_approval", outcome="approval_required"))
        assert (asked.outcome, asked.reason) == ("ignored", "an approval request is not a failure")
        assert await ev.store.all("SELECT * FROM processed_event") == []
        assert await denials(ev, tid) == []
        # The agent changed the approved action: the gateway refused its token.
        reused = await mine(ev, violation(tid, decision="require_approval", outcome="approval_refused"))
        assert reused.outcome == "created"
        assert [d[2] for d in await denials(ev, tid)] == ["approval_refused"]


async def test_runtime_denials_outside_production_or_without_a_trace_are_not_mined() -> None:
    async with evaluation_stack() as ev:
        ok = clean(load("duplicate-refund"))
        tid = ok["trace"]["trace_id"]
        ev.traces.details[tid] = ok
        staged = await mine(ev, violation(tid, environment="staging"))
        assert (staged.outcome, staged.reason) == ("ignored", "a staging call, not a production one")
        untraced = await mine(ev, violation(None))
        assert (untraced.outcome, untraced.reason) == ("ignored", "the call named no trace")
        simulated = variant(ok, source="simulation", simulation_run_id=str(uuid.uuid4()))
        ev.traces.details[simulated["trace"]["trace_id"]] = simulated
        assert (await mine(ev, violation(simulated["trace"]["trace_id"]))).outcome == "ignored"
        assert await ev.store.all("SELECT * FROM regression_runtime_denial") == []
        assert await ev.store.all("SELECT * FROM processed_event") == []
        # Unnamed environment: the trace decides.
        assert (await mine(ev, violation(tid, environment=None))).outcome == "created"


async def test_a_denial_the_agent_recorded_too_is_counted_once() -> None:
    async with evaluation_stack() as ev:
        ok = clean(load("duplicate-refund"))
        recorded = variant(
            ok, signals=["policy_denied"], summary={"violations": ["policy_denied:refund_payment"]}
        )
        tid = recorded["trace"]["trace_id"]
        ev.traces.details[tid] = recorded
        first = await mine(ev, ingested(recorded))
        assert first.outcome == "created"
        assert (await mine(ev, violation(tid))).outcome == "updated"
        [o] = await occurrences(ev, first.group_id)
        assert o["observation"]["violations"] == ["policy_denied:refund_payment"]
        assert o["reasons"] == ["a policy denied one of its actions"]


async def test_the_worker_hands_runtime_denials_to_the_miner() -> None:
    async with evaluation_stack() as ev:
        ok = clean(load("duplicate-refund"))
        tid = ok["trace"]["trace_id"]
        ev.traces.details[tid] = ok
        await ev.worker.on_event(violation(tid))
        [g] = await groups(ev)
        assert g["taxonomy"] == "POLICY_VIOLATION"


async def test_a_malformed_runtime_denial_is_parked() -> None:
    async with evaluation_stack() as ev:
        assert ev.worker.miner is not None
        env = violation("f" * 32)
        broken = replace(env, payload={**env.payload, "tool": " "})
        with pytest.raises(Permanent):
            await ev.worker.miner.on_event(broken)
        nameless = replace(env, project_id=None)
        with pytest.raises(Permanent):
            await ev.worker.miner.on_event(nameless)
        bad_trace = replace(env, payload={**env.payload, "trace_id": "not-a-trace"})
        with pytest.raises(Permanent):
            await ev.worker.miner.on_event(bad_trace)


# ---------------------------------------------------------------- fixed and reopened


async def test_a_passing_candidate_fixes_the_regression_and_a_later_failure_reopens_it() -> None:
    async with evaluation_stack() as ev:
        dup = load("duplicate-refund")
        await mine(ev, ingested(dup))
        [g] = await groups(ev)
        await promote(ev, g["id"], "regression-refund-payment-took-effect-twice-00abcd")
        miner = ev.worker.miner
        assert miner is not None
        run = {
            "id": new_id(),
            "organization_id": ORG,
            "project_id": PROJECT,
            "agent_name": AGENT,
            "candidate_version": "1.3.1",
        }

        def case(name: str, status: str) -> dict[str, Any]:
            return {"scenario_name": name, "comparison": {"candidate": {"status": status}}}

        # A failure of a later version mined before the fix is recorded.
        late = variant(dup, agent_version="1.3.4")
        await mine(ev, ingested(late))
        name = "regression-refund-payment-took-effect-twice-00abcd"
        async with transaction(ev.pool) as conn:
            # Another agent's run, or a failing candidate, fixes nothing.
            assert await miner.record_fixes(conn, run | {"agent_name": "other"}, [case(name, "PASSED")]) == []
            assert await miner.record_fixes(conn, run, [case(name, "FAILED")]) == []
            assert await miner.record_fixes(conn, run, [case(name, "PASSED")]) == [str(g["id"])]
        [fixed] = await groups(ev)
        assert (fixed["status"], fixed["fixed_version"], str(fixed["fixed_eval_run_id"])) == (
            "FIXED",
            "1.3.1",
            run["id"],
        )
        assert (await events(ev, g["id"]))[-1] == ("fixed", "PROMOTED", "FIXED", EVALUATION_ACTOR)
        async with transaction(ev.pool) as conn:
            # Passing again changes nothing.
            assert await miner.record_fixes(conn, run | {"id": new_id()}, [case(name, "PASSED")]) == []

        # What is learnt later about a failure already counted is not a new
        # failure: it does not reopen the fix.
        ev.traces.details[late["trace"]["trace_id"]] = late
        assert (await mine(ev, outcome_recorded(late))).outcome == "updated"
        assert (await groups(ev))[0]["status"] == "FIXED"
        # The old version failing again is not news.
        await mine(ev, ingested(variant(dup, agent_version="1.3.0")))
        assert (await groups(ev))[0]["status"] == "FIXED"
        # The fixed version failing again reopens it.
        mined = await mine(ev, ingested(variant(dup, agent_version="1.3.1")))
        [reopened] = await groups(ev)
        assert (mined.outcome, reopened["status"], reopened["occurrence_count"]) == ("joined", "REOPENED", 4)
        assert (await events(ev, g["id"]))[-1] == ("reopen", "FIXED", "REOPENED", MINER_ACTOR)
        # A later release passes it again.
        async with transaction(ev.pool) as conn:
            later = run | {"id": new_id(), "candidate_version": "1.3.2"}
            await miner.record_fixes(conn, later, [case(name, "PASSED")])
        assert [(r["status"], r["fixed_version"]) for r in await groups(ev)] == [("FIXED", "1.3.2")]
        # A regression reopened and then dismissed keeps its test but is not
        # fixed by it.
        async with transaction(ev.pool) as conn:
            await conn.execute("UPDATE regression_group SET status = 'DISMISSED' WHERE id = %s", (g["id"],))
            assert await miner.record_fixes(conn, later | {"id": new_id()}, [case(name, "PASSED")]) == []
            await conn.execute("UPDATE regression_group SET status = 'FIXED' WHERE id = %s", (g["id"],))
        # Dismissed noise stays dismissed however often it happens.
        await mine(ev, ingested(load("cross-tenant-denied")))
        denied = (await groups(ev))[1]
        async with transaction(ev.pool) as conn:
            await conn.execute(
                "UPDATE regression_group SET status = 'DISMISSED' WHERE id = %s", (denied["id"],)
            )
        await mine(ev, ingested(variant(load("cross-tenant-denied"))))
        assert [(r["status"], r["occurrence_count"]) for r in await groups(ev)][1] == ("DISMISSED", 2)


async def test_an_evaluation_run_fixes_the_regressions_its_candidate_passes() -> None:
    async with evaluation_with_simulation() as (ev, sim):
        dup = load("duplicate-refund")
        await mine(ev, ingested(dup))
        await mine(ev, ingested(load("cross-tenant-denied")))
        passing, failing = await groups(ev)
        # 1.2.4 against 1.3.0: 1.3.0 still fails the timeout scenario and
        # passes the cross-tenant one.
        await promote(ev, failing["id"], "refund-timeout-after-mutation")
        await promote(ev, passing["id"], "cross-tenant-order")
        run = await ev.ok(
            "POST",
            "/api/v1/eval-runs",
            {
                "project_id": PROJECT,
                "agent": AGENT,
                "baseline_version": "1.2.4",
                "candidate_version": "1.3.0",
                "scenarios": ["refund-timeout-after-mutation", "cross-tenant-order"],
            },
            status=202,
        )
        run_id = run["run"]["id"]
        assert await ev.worker.process_next() == run_id
        await run_simulations(sim)
        await ev.worker.check_waiting()
        out = await ev.ok("GET", f"/api/v1/eval-runs/{run_id}")
        assert out["run"]["status"] == "COMPLETED", out["run"]["error"]
        status = {c["scenario_name"]: c["candidate"]["status"] for c in out["cases"]}
        assert status == {"refund-timeout-after-mutation": "FAILED", "cross-tenant-order": "PASSED"}
        by_id = {r["id"]: r for r in await groups(ev)}
        assert (by_id[passing["id"]]["status"], by_id[passing["id"]]["fixed_version"]) == ("FIXED", "1.3.0")
        assert str(by_id[passing["id"]]["fixed_eval_run_id"]) == run_id
        assert by_id[failing["id"]]["status"] == "PROMOTED"
