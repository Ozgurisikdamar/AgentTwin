"""The runtime gateway client against a local stub of the gateway.

The stub answers as the runtime gateway's contract documents
(``packages/contracts/openapi/runtime-gateway.openapi.yaml``); every exchange
is checked against it, so a request the gateway would reject, or an answer it
could not give, fails the test that sees it.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from agenttwin.gateway import GATEWAY_CODES, Gateway, GatewayError, Refusal
from agenttwin_core import api_fakes as fake

KEY = "atk_test0000_secret-value-that-must-not-leak"
APPROVAL = fake.uuid(0xF101)
DECISION = fake.uuid(0xF102)
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


class Stub:
    """A runtime gateway with a refund policy: refunds above 100 need a
    person's approval, above 500 are denied."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.checker = fake.ExchangeChecker()
        self.approval_states = ["PENDING", "PENDING", "APPROVED"]
        self.claim_refusal: tuple[int, dict[str, Any]] | None = None

    def handle(self, h: BaseHTTPRequestHandler) -> tuple[int, dict[str, str], bytes, str]:
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(length) if length else b""
        self.requests.append(
            {"method": h.command, "path": h.path, "headers": dict(h.headers.items()), "body": body}
        )
        status, headers, payload = self.answer(h, body)
        content_type = headers.pop("Content-Type", "application/json")
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.checker.check(h.command, h.path, dict(h.headers.items()), body, status, json.loads(raw))
        return status, headers, raw, content_type

    def answer(self, h: BaseHTTPRequestHandler, body: bytes) -> tuple[int, dict[str, str], Any]:
        if h.headers.get("X-AgentTwin-Api-Key") != KEY:
            return 401, {}, fake.error("UNAUTHENTICATED", "Authentication is required.")
        if h.path == f"/gateway/v1/approvals/{APPROVAL}":
            state = self.approval_states.pop(0) if len(self.approval_states) > 1 else self.approval_states[0]
            return 200, {}, fake.approval(APPROVAL, status=state)
        if h.path == f"/gateway/v1/approvals/{APPROVAL}/token":
            if self.claim_refusal:
                return self.claim_refusal[0], {}, self.claim_refusal[1]
            return 201, {"Cache-Control": "no-store"}, fake.approval_token(APPROVAL)
        if h.path == "/gateway/v1/tools/lookup_order":
            return (
                403,
                {"X-AgentTwin-Decision": "allow", "X-AgentTwin-Decision-Id": DECISION},
                fake.error("ACCESS_DENIED", "This record belongs to another tenant."),
            )
        if h.path != "/gateway/v1/tools/refund_payment":
            return 404, {}, fake.error("TOOL_NOT_REGISTERED", "No such tool.", tool="x")
        args = json.loads(body)
        decided = {
            "X-AgentTwin-Decision-Id": DECISION,
            "X-AgentTwin-Policy": "refund-limits",
            "X-AgentTwin-Policy-Version": "2",
        }
        if args["amount"] > 500:
            return (
                403,
                decided | {"X-AgentTwin-Decision": "deny", "X-AgentTwin-Policy-Rule": "never-above-500"},
                (
                    fake.error(
                        "POLICY_DENIED", "Refunds above 500 are never automatic.", rule="never-above-500"
                    )
                ),
            )
        if args["amount"] > 100 and h.headers.get("X-AgentTwin-Approval-Token") != "apt_" + "T" * 43:
            return (
                403,
                decided | {"X-AgentTwin-Decision": "require_approval"},
                fake.error(
                    "APPROVAL_REQUIRED",
                    "A person must approve this call.",
                    approval_id=APPROVAL,
                    expires_at="2026-01-01T00:20:00Z",
                ),
            )
        replayed = (
            {"Idempotent-Replayed": "true"} if h.headers.get("Idempotency-Key") == "done-before" else {}
        )
        return (
            200,
            decided | {"X-AgentTwin-Decision": "allow"} | replayed,
            {"result": {"refund_id": "RF-1", "status": "refunded"}},
        )


@pytest.fixture
def stub() -> Iterator[tuple[Stub, str]]:
    s = Stub()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

        def _serve(self) -> None:
            status, headers, raw, content_type = s.handle(self)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = _serve

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield s, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    assert s.checker.violations == []


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def gateway(url: str, clock: Clock | None = None) -> Gateway:
    c = clock or Clock()
    return Gateway(url, KEY, agent="support-refund-agent", agent_version="1.3.1", sleep=c.sleep, clock=c.now)


def test_an_allowed_call_returns_the_tools_answer_and_the_decision(stub: tuple[Stub, str]) -> None:
    s, url = stub
    r = gateway(url).call(
        "refund_payment",
        {"order_id": "ORD-1001", "amount": 40},
        idempotency_key="refund-ORD-1001",
        traceparent=TRACEPARENT,
        headers={"X-AgentTwin-Tenant": "demo-co"},
    )
    assert r.ok and r.status == 200 and r.json() == {"result": {"refund_id": "RF-1", "status": "refunded"}}
    assert (r.decision.effect, r.decision.id, r.decision.policy, r.decision.policy_version) == (
        "allow",
        DECISION,
        "refund-limits",
        "2",
    )
    sent = s.requests[0]
    assert json.loads(sent["body"]) == {"order_id": "ORD-1001", "amount": 40}
    h = {k.lower(): v for k, v in sent["headers"].items()}
    assert h["idempotency-key"] == "refund-ORD-1001" and h["traceparent"] == TRACEPARENT
    assert h["x-agenttwin-agent"] == "support-refund-agent" and h["x-agenttwin-agent-version"] == "1.3.1"
    assert h["x-agenttwin-tenant"] == "demo-co" and "x-agenttwin-approval-token" not in h


def test_a_tools_own_error_is_its_answer_not_a_refusal(stub: tuple[Stub, str]) -> None:
    _, url = stub
    r = gateway(url).call("lookup_order", {"order_id": "ORD-9"})
    assert r.status == 403 and r.refusal is None and not r.ok and r.decision.effect == "allow"
    assert r.json()["error"]["code"] == "ACCESS_DENIED"


def test_a_denied_call_is_a_refusal_with_the_rule(stub: tuple[Stub, str]) -> None:
    _, url = stub
    r = gateway(url).call("refund_payment", {"order_id": "ORD-1003", "amount": 900}, idempotency_key="k-900")
    assert (
        r.refusal is not None
        and r.refusal.code == "POLICY_DENIED"
        and r.refusal.details["rule"] == "never-above-500"
    )
    assert r.decision.effect == "deny" and r.decision.rule == "never-above-500"


def test_an_approved_call_is_repeated_with_the_token_and_the_same_key(stub: tuple[Stub, str]) -> None:
    s, url = stub
    clock = Clock()
    seen = []
    r = gateway(url, clock).call_approved(
        "refund_payment",
        {"order_id": "ORD-1003", "amount": 150},
        idempotency_key="refund-ORD-1003",
        wait_s=10,
        interval_s=2,
        on_response=seen.append,
    )
    assert r.ok and r.json()["result"]["status"] == "refunded" and r.approval is not None
    assert r.approval["status"] == "APPROVED" and clock.sleeps == [2, 2]
    assert [x.decision.effect for x in seen] == ["require_approval", "allow"]
    calls = [q for q in s.requests if q["path"] == "/gateway/v1/tools/refund_payment"]
    assert len(calls) == 2 and calls[0]["body"] == calls[1]["body"]
    keys = [{k.lower(): v for k, v in c["headers"].items()}.get("idempotency-key") for c in calls]
    tokens = [
        {k.lower(): v for k, v in c["headers"].items()}.get("x-agenttwin-approval-token") for c in calls
    ]
    assert keys == ["refund-ORD-1003", "refund-ORD-1003"] and tokens == [None, "apt_" + "T" * 43]


def test_nobody_decides_in_time_and_the_request_stays_open(stub: tuple[Stub, str]) -> None:
    s, url = stub
    s.approval_states = ["PENDING"]
    clock = Clock()
    r = gateway(url, clock).call_approved(
        "refund_payment", {"order_id": "ORD-1003", "amount": 150}, idempotency_key="k", wait_s=5, interval_s=2
    )
    assert (
        r.refusal is not None and r.refusal.code == "APPROVAL_REQUIRED" and r.refusal.approval_id == APPROVAL
    )
    assert r.approval is not None and r.approval["status"] == "PENDING"
    assert clock.sleeps == [2, 2, 1] and not any(q["path"].endswith("/token") for q in s.requests)


def test_without_waiting_there_is_one_look(stub: tuple[Stub, str]) -> None:
    s, url = stub
    s.approval_states = ["PENDING"]
    clock = Clock()
    r = gateway(url, clock).call_approved("refund_payment", {"order_id": "ORD-1003", "amount": 150})
    assert r.refusal is not None and r.approval is not None and clock.sleeps == []
    assert sum(q["path"] == f"/gateway/v1/approvals/{APPROVAL}" for q in s.requests) == 1


@pytest.mark.parametrize("state", ["DENIED", "EXPIRED"])
def test_a_denied_or_expired_request_is_not_claimed(stub: tuple[Stub, str], state: str) -> None:
    s, url = stub
    s.approval_states = [state]
    r = gateway(url).call_approved("refund_payment", {"order_id": "ORD-1003", "amount": 150}, wait_s=30)
    assert r.refusal is not None and r.approval is not None and r.approval["status"] == state
    assert not any(q["path"].endswith("/token") for q in s.requests)


def test_a_refused_claim_is_reported_with_the_approval(stub: tuple[Stub, str]) -> None:
    s, url = stub
    s.approval_states = ["APPROVED"]
    s.claim_refusal = (403, fake.error("APPROVAL_EXPIRED", "The approval has expired.", approval_id=APPROVAL))
    r = gateway(url).call_approved("refund_payment", {"order_id": "ORD-1003", "amount": 150}, wait_s=1)
    assert r.refusal is not None and r.refusal.code == "APPROVAL_REQUIRED"
    assert r.approval is not None and r.approval["claim_refused"] == "APPROVAL_EXPIRED"
    with pytest.raises(GatewayError) as err:
        gateway(url).claim_token(APPROVAL)
    assert (err.value.status, err.value.code, err.value.details) == (
        403,
        "APPROVAL_EXPIRED",
        {"approval_id": APPROVAL},
    )


def test_no_answer_is_an_error_and_the_key_never_leaks() -> None:
    g = Gateway("http://127.0.0.1:9", KEY, timeout_s=2)
    with pytest.raises(GatewayError) as err:
        g.call("refund_payment", {"amount": 1})
    assert err.value.status == 0 and err.value.code == "UNAVAILABLE"
    assert KEY not in str(err.value) and KEY not in repr(g)
    with pytest.raises(ValueError):
        Gateway("ftp://x", KEY)
    with pytest.raises(ValueError):
        Gateway("http://x", "")


def test_a_token_is_not_printed(stub: tuple[Stub, str]) -> None:
    _, url = stub
    s, _ = stub
    s.approval_states = ["APPROVED"]
    token = gateway(url).claim_token(APPROVAL)
    assert token.token.startswith("apt_") and token.token not in repr(token)


def test_the_refusal_codes_are_the_ones_the_contract_documents() -> None:
    doc = yaml.safe_load(
        (
            Path(__file__).resolve().parents[3] / "packages/contracts/openapi/runtime-gateway.openapi.yaml"
        ).read_text()
    )
    op = doc["paths"]["/gateway/v1/tools/{tool}"]["post"]
    text = yaml.dump(op)
    for response in op["responses"].values():
        if "$ref" in response:
            text += yaml.dump(doc["components"]["responses"][response["$ref"].split("/")[-1]])
    assert set(re.findall(r"`([A-Z][A-Z_]{4,})`", text)) == GATEWAY_CODES


def test_a_replay_says_so(stub: tuple[Stub, str]) -> None:
    _, url = stub
    g = gateway(url)
    assert not g.call(
        "refund_payment", {"order_id": "ORD-1", "amount": 40}, idempotency_key="k1"
    ).decision.replayed
    again = g.call("refund_payment", {"order_id": "ORD-1", "amount": 40}, idempotency_key="done-before")
    assert again.ok and again.decision.replayed


def test_an_approval_id_must_be_a_string() -> None:
    assert Refusal("APPROVAL_REQUIRED", "", {"approval_id": 7}).approval_id is None
    assert Refusal("APPROVAL_REQUIRED", "", {"approval_id": APPROVAL}).approval_id == APPROVAL


def test_losing_the_gateway_while_claiming_is_an_error(
    stub: tuple[Stub, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without an answer nobody knows whether the token was issued: the
    caller must see the error rather than a refusal it could act on."""
    s, url = stub
    s.approval_states = ["APPROVED"]
    g = gateway(url)

    def lost(_: str) -> Any:
        raise GatewayError(0, "UNAVAILABLE", "no answer")

    monkeypatch.setattr(g, "claim_token", lost)
    with pytest.raises(GatewayError) as err:
        g.call_approved("refund_payment", {"order_id": "ORD-1003", "amount": 150}, wait_s=1)
    assert err.value.status == 0
