"""The demo agent contained by a runtime gateway (ADR-0033).

A contained run calls the real Demo Co tools through a stand-in for the
runtime gateway that applies the demo's refund policy (above 100 needs a
person's approval, above 500 is denied) and answers as the gateway's
contract documents; every exchange is checked against it.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agenttwin import AgentTwin, Config
from agenttwin_core import api_fakes as fake
from support_refund_agent.agent import (
    Agent,
    ContainmentUnavailable,
    ManifestStore,
    RunRequest,
    scripted_model_factory,
)
from support_refund_agent.server import AgentServer
from support_refund_agent.tools_server import ToolsServer
from support_refund_agent.world import INTERNAL_API_KEY

FIXED = "1.3.1"
KEY = "atk_test0000_secret-value-that-must-not-leak"
TOKEN = "apt_" + "T" * 43


class Gateway:
    """The refund policy in front of the Demo Co tools."""

    def __init__(self, tools_url: str) -> None:
        self.tools_url = tools_url
        self.checker = fake.ExchangeChecker()
        self.deny_above = 500.0
        self.tool_times_out = False
        self.decide = "APPROVED"  # what the person decides...
        self.after_polls = 2  # ...after this many looks at the request
        self.polls = 0
        self.approved_args: bytes | None = None
        self.calls: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def handle(self, h: BaseHTTPRequestHandler) -> tuple[int, dict[str, str], dict[str, Any]]:
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(length) if length else b""
        status, headers, payload = self.answer(h, body)
        self.checker.check(h.command, h.path, dict(h.headers.items()), body, status, payload)
        return status, headers, payload

    def approval(self) -> dict[str, Any]:
        status = "PENDING" if self.polls < self.after_polls else self.decide
        extra = {"decision_reason": "no receipt", "decided_by": "user:reviewer"} if status == "DENIED" else {}
        return fake.approval(fake.uuid(0xA1), status=status, **extra)

    def answer(self, h: BaseHTTPRequestHandler, body: bytes) -> tuple[int, dict[str, str], dict[str, Any]]:
        if h.headers.get("X-AgentTwin-Api-Key") != KEY:
            return 401, {}, fake.error("UNAUTHENTICATED", "Authentication is required.")
        approval_path = f"/gateway/v1/approvals/{fake.uuid(0xA1)}"
        with self.lock:
            if h.path == approval_path:
                self.polls += 1
                return 200, {}, self.approval()
            if h.path == approval_path + "/token":
                return 201, {"Cache-Control": "no-store"}, fake.approval_token(fake.uuid(0xA1))
        tool = h.path.rsplit("/", 1)[-1]
        args = json.loads(body or b"{}")
        self.calls.append(
            {"tool": tool, "args": args, "headers": {k.lower(): v for k, v in h.headers.items()}}
        )
        decided = {"X-AgentTwin-Decision-Id": fake.uuid(0xD0 + len(self.calls))}
        if tool == "refund_payment" and self.tool_times_out:
            return (
                504,
                decided | {"X-AgentTwin-Decision": "allow"},
                fake.error("TOOL_TIMEOUT", "The tool did not answer within 10000 ms."),
            )
        if tool == "refund_payment":
            decided |= {"X-AgentTwin-Policy": "refund-limits", "X-AgentTwin-Policy-Version": "1"}
            amount = float(args.get("amount", 0))
            if amount > self.deny_above:
                return (
                    403,
                    decided | {"X-AgentTwin-Decision": "deny", "X-AgentTwin-Policy-Rule": "never-above-500"},
                    (
                        fake.error(
                            "POLICY_DENIED", "Refunds above 500 are never automatic.", rule="never-above-500"
                        )
                    ),
                )
            if amount > 100:
                if h.headers.get("X-AgentTwin-Approval-Token") != TOKEN:
                    self.approved_args = body
                    return (
                        403,
                        decided | {"X-AgentTwin-Decision": "require_approval"},
                        fake.error(
                            "APPROVAL_REQUIRED",
                            "Refunds above 100 need a person's approval.",
                            approval_id=fake.uuid(0xA1),
                            expires_at="2026-01-01T00:20:00Z",
                        ),
                    )
                assert body == self.approved_args, "the approved action is repeated exactly"
        # Forwarded to the tool, with the headers its endpoint lists.
        req = urllib.request.Request(
            f"{self.tools_url}/tools/{tool}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-AgentTwin-Tenant": h.headers["X-AgentTwin-Tenant"],
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, decided | {"X-AgentTwin-Decision": "allow"}, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, decided | {"X-AgentTwin-Decision": "allow"}, json.loads(err.read())


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def telemetry(exporter: InMemorySpanExporter) -> Iterator[AgentTwin]:
    client = AgentTwin(Config(service_name="support-refund-agent", schedule_delay_ms=20), exporter=exporter)
    yield client
    client.shutdown()


@pytest.fixture
def stack() -> Iterator[tuple[ToolsServer, Gateway, str]]:
    tools = ToolsServer().start()
    gw = Gateway(tools.url)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

        def _serve(self) -> None:
            status, headers, payload = gw.handle(self)
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = _serve

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield tools, gw, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    tools.stop()
    assert gw.checker.violations == []


def agent(telemetry: AgentTwin, tools_url: str, gateway_url: str | None, wait_s: float = 5.0) -> Agent:
    return Agent(
        telemetry,
        ManifestStore(),
        tools_base_url=tools_url,
        model_factory=scripted_model_factory(INTERNAL_API_KEY),
        tool_timeout_s=2.0,
        backoff_scale=0.01,
        gateway_url=gateway_url,
        gateway_api_key=KEY,
        approval_wait_s=wait_s,
    )


def contained(text: str, customer: str = "CUS-100") -> RunRequest:
    return RunRequest(input=text, customer_id=customer, version=FIXED, contained=True)


def refunds(result: Any) -> list[dict[str, Any]]:
    return [c for c in result.tool_calls if c["name"] == "refund_payment"]


def test_an_approved_over_limit_refund_runs_once(
    telemetry: AgentTwin, exporter: InMemorySpanExporter, stack: tuple[ToolsServer, Gateway, str]
) -> None:
    tools, gw, url = stack
    result = agent(telemetry, tools.url, url).run(contained("Please refund $150 for ORD-1001"))
    # Contained, the agent asks for the refund; the gateway held it until a
    # person approved, then forwarded it once.
    assert result.business_outcome == "REFUND_COMPLETED", result.output
    assert [c["status"] for c in refunds(result)] == ["ok"]
    assert tools.world.snapshot()["orders"]["ORD-1001"] == {"refunded_amount": 150.0, "refund_count": 1}
    forwarded = [c for c in gw.calls if c["tool"] == "refund_payment"]
    assert len(forwarded) == 2 and forwarded[0]["args"] == forwarded[1]["args"]
    keys = {c["headers"].get("idempotency-key") for c in forwarded}
    assert keys == {"refund-ORD-1001-150.00"}
    assert {c["headers"].get("x-agenttwin-agent") for c in gw.calls} == {"support-refund-agent"}
    assert {c["headers"].get("x-agenttwin-agent-version") for c in gw.calls} == {FIXED}

    telemetry.flush()
    spans = exporter.get_finished_spans()
    refund_span = next(s for s in spans if s.name == "execute_tool refund_payment")
    decisions = [
        s for s in spans if s.name == "policy.decision" and s.parent.span_id == refund_span.context.span_id
    ]
    effects = [
        (
            dict(s.attributes)["agenttwin.policy.decision"],
            dict(s.attributes).get("agenttwin.policy.approval_id"),
        )
        for s in decisions
    ]
    assert effects == [("require_approval", fake.uuid(0xA1)), ("allow", None)]
    # The gateway joined the tool call's trace.
    parents = {c["headers"]["traceparent"].split("-")[1] for c in forwarded}
    assert parents == {format(refund_span.context.trace_id, "032x")}
    root = next(s for s in spans if s.name.startswith("invoke_agent"))
    assert dict(root.attributes)["agenttwin.runtime.contained"] is True


def test_a_refund_nobody_approved_in_time_is_waiting(
    telemetry: AgentTwin, stack: tuple[ToolsServer, Gateway, str]
) -> None:
    tools, gw, url = stack
    gw.after_polls = 10**6
    result = agent(telemetry, tools.url, url, wait_s=0).run(contained("Please refund $150 for ORD-1001"))
    assert result.business_outcome == "REFUND_AWAITING_APPROVAL" and "approval" in result.output
    call = refunds(result)[0]
    assert (call["status"], call["error_code"]) == ("denied", "APPROVAL_PENDING")
    assert tools.world.snapshot()["refunds"] == []


def test_a_declined_refund_says_why(telemetry: AgentTwin, stack: tuple[ToolsServer, Gateway, str]) -> None:
    tools, gw, url = stack
    gw.decide, gw.after_polls = "DENIED", 0
    result = agent(telemetry, tools.url, url).run(contained("Please refund $150 for ORD-1001"))
    assert result.business_outcome == "REFUND_DENIED" and "no receipt" in result.output
    assert refunds(result)[0]["error_code"] == "APPROVAL_DENIED"
    assert tools.world.snapshot()["refunds"] == []


def test_the_policy_denies_what_no_person_may_approve(
    telemetry: AgentTwin, stack: tuple[ToolsServer, Gateway, str]
) -> None:
    tools, gw, url = stack
    gw.deny_above = 400.0
    result = agent(telemetry, tools.url, url).run(
        contained("Please refund $450 for ORD-1002", customer="CUS-101")
    )
    assert result.business_outcome == "REFUND_DENIED" and "never automatic" in result.output
    assert refunds(result)[0]["error_code"] == "POLICY_DENIED" and gw.polls == 0
    assert tools.world.snapshot()["refunds"] == []


def test_a_small_refund_is_simply_allowed(
    telemetry: AgentTwin, stack: tuple[ToolsServer, Gateway, str]
) -> None:
    tools, gw, url = stack
    result = agent(telemetry, tools.url, url).run(contained("Please refund $50 for ORD-1001"))
    assert result.business_outcome == "REFUND_COMPLETED"
    assert [c["tool"] for c in gw.calls if c["tool"] == "refund_payment"] == ["refund_payment"]
    assert gw.polls == 0


def test_uncontained_runs_do_not_touch_the_gateway(
    telemetry: AgentTwin, stack: tuple[ToolsServer, Gateway, str]
) -> None:
    tools, gw, url = stack
    result = agent(telemetry, tools.url, url).run(
        RunRequest(input="Please refund $150 for ORD-1001", customer_id="CUS-100", version=FIXED)
    )
    assert result.business_outcome == "REFUND_ESCALATED" and gw.calls == []


def test_containment_without_a_gateway_is_refused(telemetry: AgentTwin) -> None:
    tools = ToolsServer().start()
    try:
        a = agent(telemetry, tools.url, None)
        with pytest.raises(ContainmentUnavailable):
            a.run(contained("Please refund $150 for ORD-1001"))
        server = AgentServer(a).start()
        try:
            req = urllib.request.Request(
                server.url + "/run",
                data=json.dumps({"input": "refund ORD-1001", "contained": True}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(req, timeout=5)
            assert err.value.code == 409
            assert json.loads(err.value.read())["error"]["code"] == "CONTAINMENT_UNAVAILABLE"
            bad = json.dumps({"input": "refund ORD-1001", "contained": "yes"}).encode()
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(
                    urllib.request.Request(
                        server.url + "/run",
                        data=bad,
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    ),
                    timeout=5,
                )
            assert err.value.code == 400
        finally:
            server.stop()
    finally:
        tools.stop()


def test_a_tool_timeout_behind_the_gateway_is_a_timeout(
    telemetry: AgentTwin, stack: tuple[ToolsServer, Gateway, str]
) -> None:
    """The fixed agent checks the order before retrying a timed-out refund,
    exactly as it does without the gateway."""
    tools, gw, url = stack
    gw.tool_times_out = True
    result = agent(telemetry, tools.url, url).run(contained("Please refund $50 for ORD-1001"))
    attempts = refunds(result)
    assert [(c["status"], c["error_code"]) for c in attempts] == [("timeout", "TOOL_TIMEOUT")] * len(attempts)
    names = [c["name"] for c in result.tool_calls]
    assert names[names.index("refund_payment") + 1] == "lookup_order"


def test_an_unreachable_gateway_is_reported_not_bypassed(telemetry: AgentTwin) -> None:
    tools = ToolsServer().start()
    try:
        result = agent(telemetry, tools.url, "http://127.0.0.1:9").run(
            contained("Please refund $50 for ORD-1001")
        )
    finally:
        tools.stop()
    assert result.tool_calls and result.tool_calls[0]["error_code"] == "GATEWAY_UNREACHABLE"
    assert tools.world.snapshot()["refunds"] == []


def test_a_tools_own_refusal_passes_through_the_gateway(
    telemetry: AgentTwin, stack: tuple[ToolsServer, Gateway, str]
) -> None:
    tools, _, url = stack
    result = agent(telemetry, tools.url, url).run(contained("Refund $60 for ORD-2001"))
    assert [c["name"] for c in result.tool_calls] == ["lookup_order"]
    assert (result.tool_calls[0]["status"], result.tool_calls[0]["error_code"]) == ("denied", "ACCESS_DENIED")
    assert result.business_outcome == "ORDER_NOT_ACCESSIBLE" and "Mallory" not in result.output
