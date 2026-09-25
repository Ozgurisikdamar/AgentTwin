"""The regressions API's choices that need no database: which twin a draft
runs against."""

from __future__ import annotations

from typing import Any, cast

import pytest

from agenttwin_core.logx import get_logger
from agenttwin_evaluation.clients import SimulationClient, TraceClient
from agenttwin_evaluation.regression_store import RegressionStore
from agenttwin_evaluation.regressions import RegressionsAPI
from agenttwin_evaluation.store import Store

pytestmark = pytest.mark.anyio


class _Simulation:
    def __init__(self, scenarios: list[dict[str, Any]]) -> None:
        self.listed = scenarios
        self.read: list[str] = []

    async def scenarios(self, org: str, project: str) -> list[dict[str, Any]]:
        return self.listed

    async def twin_document(self, org: str, project: str, name: str) -> dict[str, Any] | None:
        self.read.append(name)
        return {"metadata": {"name": name}}


def api(sim: _Simulation) -> RegressionsAPI:
    return RegressionsAPI(
        store=cast(Store, None),
        regressions=cast(RegressionStore, None),
        simulation=cast(SimulationClient, sim),
        traces=cast(TraceClient, None),
        log=get_logger("test"),
    )


def s(agent: str, twin: str | None) -> dict[str, Any]:
    return {"agent": agent, "twin": twin}


async def twin_for(agent: str, *scenarios: dict[str, Any]) -> str | None:
    sim = _Simulation(list(scenarios))
    doc = await api(sim)._twin("org", "project", agent)
    assert sim.read == ([] if doc is None else [doc["metadata"]["name"]])
    return None if doc is None else str(doc["metadata"]["name"])


async def test_the_twin_is_the_one_the_agents_scenarios_use_most() -> None:
    # Another agent's twin is used more; the agent's own wins.
    scenarios = [s("a", "t2"), s("a", "t1"), s("a", "t1"), *[s("b", "t3")] * 5]
    assert await twin_for("a", *scenarios) == "t1"


async def test_a_tie_goes_to_the_first_name() -> None:
    assert await twin_for("a", s("a", "t2"), s("a", "t1")) == "t1"


async def test_without_a_scenario_of_the_agent_the_projects_most_used_twin_is_taken() -> None:
    assert await twin_for("a", s("b", "t4"), s("b", "t3"), s("c", "t3")) == "t3"
    # Scenarios of the agent without a twin do not count as the agent's.
    assert await twin_for("a", s("a", None), s("b", "t4")) == "t4"


async def test_without_any_twin_there_is_none() -> None:
    assert await twin_for("a") is None
    assert await twin_for("a", s("a", None), s("b", "")) is None
