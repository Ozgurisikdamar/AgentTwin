from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, tzinfo
from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agenttwin import AgentTwin, Config
from support_refund_agent import world as world_module
from support_refund_agent.agent import Agent, ManifestStore, RunRequest, scripted_model_factory
from support_refund_agent.anthropic_model import AnthropicModel, to_anthropic_messages
from support_refund_agent.models import ToolCallRequest, ToolSpec
from support_refund_agent.server import AgentServer
from support_refund_agent.tools_server import Fault, ToolsServer
from support_refund_agent.traffic import TrafficGenerator
from support_refund_agent.world import INTERNAL_API_KEY, default_world

ORDER_ID = re.compile(r"ORD-\d{3,}")


@pytest.fixture
def stack() -> Iterator[tuple[AgentServer, ToolsServer, InMemorySpanExporter]]:
    exporter = InMemorySpanExporter()
    telemetry = AgentTwin(Config(schedule_delay_ms=20), exporter=exporter)
    tools = ToolsServer(
        admin_token="admin-secret",
        faults=[Fault("refund_payment", "timeout_after_mutation", delay_s=0.5, times=1)],
    ).start()
    agent = Agent(
        telemetry,
        ManifestStore(),
        tools_base_url=tools.url,
        model_factory=scripted_model_factory(INTERNAL_API_KEY),
        tool_timeout_s=0.25,
        backoff_scale=0.01,
    )
    server = AgentServer(agent, token="agent-token").start()
    yield server, tools, exporter
    server.stop()
    tools.stop()
    telemetry.shutdown()


def post(url: str, body: Any, token: str | None = "agent-token") -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_agent_adapter_contract(stack: Any) -> None:
    server, tools, exporter = stack
    code, body = post(
        server.url + "/run",
        {
            "input": "Refund $50 for ORD-1001",
            "customer_id": "CUS-100",
            "agent_version": "1.3.0",
            "tools_base_url": tools.url,
            "run_context": {
                "source": "simulation",
                "simulation_run_id": "run-1",
                "scenario_id": "timeout-after-mutation",
            },
        },
    )
    assert code == 200, body
    assert body["agent_version"] == "1.3.0"
    assert body["model_kind"] == "deterministic-fake"
    assert len(body["trace_id"]) == 32
    assert [c["name"] for c in body["tool_calls"]] == [
        "lookup_order",
        "refund_payment",
        "refund_payment",
        "send_email",
    ]
    server.agent.telemetry.flush()
    root = next(s for s in exporter.get_finished_spans() if s.parent is None)
    attrs = dict(root.attributes or {})
    assert attrs["agenttwin.source"] == "simulation"
    assert attrs["agenttwin.simulation.run_id"] == "run-1"
    assert attrs["agenttwin.scenario.id"] == "timeout-after-mutation"


def test_agent_adapter_rejects_bad_requests(stack: Any) -> None:
    server, _, _ = stack
    assert post(server.url + "/run", {"input": "hi"}, token=None)[0] == 401
    assert post(server.url + "/run", {"input": "hi"}, token="wrong")[0] == 401
    assert post(server.url + "/run", {"input": ""})[0] == 400
    assert post(server.url + "/run", {"input": "hi", "tool_headers": {"a": 1}})[0] == 400
    code, body = post(server.url + "/run", {"input": "refund \ud800 please"})
    assert code == 400 and "lone surrogate" in body["error"]["message"]
    code, body = post(server.url + "/run", {"input": "hi", "agent_version": "9.9.9"})
    assert code == 404 and body["error"]["code"] == "UNKNOWN_VERSION"
    with urllib.request.urlopen(server.url + "/versions", timeout=5) as resp:
        assert json.loads(resp.read())["versions"] == ["1.2.3", "1.2.4", "1.3.0", "1.3.1", "1.3.2"]


def test_traffic_generator_verifies_outcomes_against_the_ledger(stack: Any) -> None:
    server, tools, _ = stack

    def run(req: RunRequest) -> dict[str, Any]:
        return server.agent.run(req).to_json()

    gen = TrafficGenerator(
        run,
        tools_url=tools.url,
        tools_admin_token="admin-secret",
        versions={"1.3.0": 1.0},
        seed=1,
        verify_outcomes=True,
    )
    records = gen.run(25)
    assert len(records) == 25
    kinds = {r["kind"] for r in records}
    assert {"refund_small", "status", "policy"} <= kinds
    verified = [r for r in records if "verified_outcome" in r]
    assert verified, "some refunds must be verified"
    # The fault makes the first refund time out after it was applied; the
    # candidate retries without an idempotency key -> the ledger shows two
    # refunds while the agent claimed success.
    duplicates = [r for r in verified if r["verified_outcome"]["refund_count"] == 2]
    assert len(duplicates) == 1
    assert duplicates[0]["claimed_outcome"] == "SUCCESS"
    assert duplicates[0]["verified_outcome"]["status"] == "FAILURE"
    assert all(r["verified_outcome"]["status"] == "SUCCESS" for r in verified if r not in duplicates)


def test_the_incident_is_reproduced_on_its_own_order(stack: Any) -> None:
    """The seeded production incident: 1.3.0 retries the timed-out refund
    without an idempotency key and pays twice, claiming success; 1.3.1
    reuses its key and pays once. The configured faults are left alone."""
    server, tools, _ = stack

    def run(req: RunRequest) -> dict[str, Any]:
        return server.agent.run(req).to_json()

    gen = TrafficGenerator(
        run,
        tools_url=tools.url,
        tools_admin_token="admin-secret",
        versions={"1.3.0": 1.0},
        verify_outcomes=True,
    )
    broken = gen.incident("1.3.0")
    assert (broken["kind"], broken["version"], broken["claimed_outcome"]) == ("incident", "1.3.0", "SUCCESS")
    assert broken["verified_outcome"]["refund_count"] == 2
    assert broken["verified_outcome"]["status"] == "FAILURE"
    fixed = gen.incident("1.3.1")
    assert fixed["verified_outcome"]["refund_count"] == 1
    assert fixed["verified_outcome"]["status"] == "SUCCESS"
    # Only the incident orders' own faults fired.
    faulted = [c for c in tools.calls if c["fault"]]
    assert [(c["tool"], c["args"]["order_id"]) for c in faulted] == [
        ("refund_payment", broken["order_id"]),
        ("refund_payment", fixed["order_id"]),
    ]


def test_tools_admin_endpoints_require_token(stack: Any) -> None:
    _, tools, _ = stack
    code, _ = post(tools.url + "/admin/orders", {"customer_id": "CUS-100", "total": 10}, token=None)
    assert code == 401
    code, order = post(
        tools.url + "/admin/orders", {"customer_id": "CUS-100", "total": 10}, token="admin-secret"
    )
    assert code == 201 and order["order_id"].startswith("ORD-")
    code, _ = post(
        tools.url + "/admin/orders",
        {"customer_id": "CUS-200", "total": 10, "tenant": "demo-co"},
        token="admin-secret",
    )
    assert code == 403, "cannot create orders for another tenant's customer"
    code, _ = post(
        tools.url + "/admin/orders",
        {"customer_id": "CUS-100", "total": 10, "inject_faults": "no"},
        token="admin-secret",
    )
    assert code == 400


class _Clock(datetime):
    """``datetime`` with a clock the test moves."""

    current = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:  # type: ignore[override]
        return cls.current


def test_orders_created_after_a_restart_get_new_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runtime gateway remembers refunds by order number, in PostgreSQL;
    a restarted tools server handing out an earlier number turned a new refund
    into a replay of an old one."""
    monkeypatch.setattr(world_module, "datetime", _Clock)
    before = default_world()
    earlier = [before.create_order("demo-co", "CUS-100", 150.0)["order_id"] for _ in range(5)]
    assert len(set(earlier)) == 5

    _Clock.current += timedelta(seconds=1)
    after = default_world()
    later = after.create_order("demo-co", "CUS-100", 150.0)["order_id"]
    assert all(int(later[4:]) > int(e[4:]) for e in earlier)
    # Still an order number the agent reads from a message, and short.
    assert ORDER_ID.fullmatch(later) and len(later[4:]) <= 10

    # A number is never one the world already has.
    after.orders[f"ORD-{int(later[4:]) + 1}"] = dict(after.orders[later])
    assert after.create_order("demo-co", "CUS-100", 1.0)["order_id"] == f"ORD-{int(later[4:]) + 2}"


def test_orders_can_be_exempted_from_injected_faults() -> None:
    always = [Fault("refund_payment", "server_error")]
    tools = ToolsServer(admin_token="admin-secret", faults=always).start()
    try:
        refund = {"amount": 5, "idempotency_key": "k1"}
        code, exempt = post(
            tools.url + "/admin/orders",
            {"customer_id": "CUS-100", "total": 50, "inject_faults": False},
            token="admin-secret",
        )
        assert code == 201
        code, normal = post(
            tools.url + "/admin/orders", {"customer_id": "CUS-100", "total": 50}, token="admin-secret"
        )
        assert code == 201
        code, body = post(
            tools.url + "/tools/refund_payment", {"order_id": exempt["order_id"], **refund}, token=None
        )
        assert code == 200 and body["result"]["status"] == "succeeded"
        code, body = post(
            tools.url + "/tools/refund_payment",
            {"order_id": normal["order_id"], **refund, "idempotency_key": "k2"},
            token=None,
        )
        assert code == 500 and body["error"]["code"] == "UPSTREAM_ERROR"
        assert [c["fault"] for c in tools.calls] == [None, "server_error"]
    finally:
        tools.stop()


def test_an_order_can_carry_its_own_faults() -> None:
    """A reproducible incident: the order's own fault fires on its first
    call of the tool only, and the configured faults never touch it."""
    always = [Fault("refund_payment", "server_error")]
    tools = ToolsServer(admin_token="admin-secret", faults=always).start()
    try:
        timeout = [{"tool": "refund_payment", "kind": "timeout_after_mutation", "delay_s": 0}]
        code, incident = post(
            tools.url + "/admin/orders",
            {"customer_id": "CUS-100", "total": 140, "faults": timeout},
            token="admin-secret",
        )
        assert code == 201
        order = incident["order_id"]
        for key in ("k1", "k2"):
            code, body = post(
                tools.url + "/tools/refund_payment",
                {"order_id": order, "amount": 40, "idempotency_key": key},
                token=None,
            )
            assert code == 200 and body["result"]["status"] == "succeeded"
        # A read of the same order is not the faulted tool.
        code, _ = post(tools.url + "/tools/lookup_order", {"order_id": order}, token=None)
        assert code == 200
        assert [c["fault"] for c in tools.calls] == ["timeout_after_mutation", None, None]
        # The first refund was applied before the (simulated) timeout: two refunds.
        assert tools.world.snapshot()["orders"][order]["refund_count"] == 2
        for bad in (
            [{"tool": "refund_payment", "kind": "explode"}],
            [{"tool": "no_such_tool", "kind": "server_error"}],
            [{"tool": "refund_payment", "kind": "server_error", "times": 0}],
            [{"tool": "refund_payment", "kind": "server_error", "delay_s": 60}],
            [{"tool": "refund_payment", "kind": "server_error", "when": "always"}],
            {"tool": "refund_payment"},
        ):
            code, body = post(
                tools.url + "/admin/orders",
                {"customer_id": "CUS-100", "total": 10, "faults": bad},
                token="admin-secret",
            )
            assert code == 400 and body["error"]["code"] == "INVALID_ORDER", bad
    finally:
        tools.stop()


class _FakeMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="Looking up your order."),
                SimpleNamespace(
                    type="tool_use", id="toolu_1", name="lookup_order", input={"order_id": "ORD-1001"}
                ),
            ],
            stop_reason="tool_use",
            usage=SimpleNamespace(input_tokens=321, output_tokens=21),
            model="claude-sonnet-5",
        )


def test_anthropic_adapter_converts_messages_and_usage() -> None:
    fake = _FakeMessages()
    model = AnthropicModel("claude-sonnet-5", client=SimpleNamespace(messages=fake))
    messages = [
        {"role": "user", "content": "Refund ORD-1001"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [ToolCallRequest("toolu_0", "lookup_order", {"order_id": "ORD-1001"})],
        },
        {"role": "tool", "tool_call_id": "toolu_0", "name": "lookup_order", "content": '{"status":"ok"}'},
    ]
    resp = model.chat("be helpful", messages, [ToolSpec("lookup_order", "Look up", {"type": "object"})])
    assert resp.tool_calls == [ToolCallRequest("toolu_1", "lookup_order", {"order_id": "ORD-1001"})]
    assert (resp.input_tokens, resp.output_tokens, resp.stop_reason) == (321, 21, "tool_use")
    sent = fake.calls[0]
    assert sent["system"] == "be helpful" and sent["temperature"] == 0
    assert sent["tools"][0]["input_schema"] == {"type": "object"}
    assert to_anthropic_messages(messages) == [
        {"role": "user", "content": "Refund ORD-1001"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_0",
                    "name": "lookup_order",
                    "input": {"order_id": "ORD-1001"},
                }
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_0", "content": '{"status":"ok"}'}],
        },
    ]
    assert sent["messages"] == to_anthropic_messages(messages)
