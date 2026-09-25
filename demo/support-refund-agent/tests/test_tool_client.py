"""The tool client under broken responses (the faults tool twins inject):
every one becomes a tool error the planner can reason about, never a crash
of the agent."""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agenttwin import AgentTwin, Config
from support_refund_agent.tool_client import ToolClient

Reply = Callable[[socket.socket], None]


def _read_request(conn: socket.socket) -> None:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            return
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    while len(body) < length:
        body += conn.recv(65536)


@contextmanager
def misbehaving_server(reply: Reply) -> Iterator[str]:
    """A raw TCP server answering every request with ``reply``."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.2)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            with conn:
                _read_request(conn)
                reply(conn)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.getsockname()[1]}"
    finally:
        stop.set()
        thread.join(timeout=5)
        srv.close()


def respond(status: str, body: bytes, *, length: int | None = None) -> Reply:
    def reply(conn: socket.socket) -> None:
        head = (
            f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body) if length is None else length}\r\nConnection: close\r\n\r\n"
        )
        conn.sendall(head.encode() + body)

    return reply


def drop(conn: socket.socket) -> None:
    """Closes the connection without answering."""


@pytest.fixture
def run() -> Iterator[Any]:
    telemetry = AgentTwin(
        Config(service_name="tool-client-test", schedule_delay_ms=20), exporter=InMemorySpanExporter()
    )
    with telemetry.agent_run("tool-client-test", "0.0.1", input="test") as agent_run:
        yield agent_run
    telemetry.shutdown()


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        (respond("200 OK", b'{"result": {"ok": tru'), "MALFORMED_RESPONSE"),
        (respond("200 OK", b"[1, 2, 3]"), "MALFORMED_RESPONSE"),
        (respond("200 OK", b'{"result": {"refund_id": "RF-1"}}', length=500), "BROKEN_RESPONSE"),
        (drop, "CONNECTION_DROPPED"),
    ],
    ids=["malformed-json", "not-an-object", "partial-body", "dropped-connection"],
)
def test_broken_responses_become_tool_errors(run: Any, reply: Reply, code: str) -> None:
    with misbehaving_server(reply) as url:
        client = ToolClient(base_url=url, tenant="demo-co", timeout_s=2.0)
        outcome = client.call(run, "refund_payment", {"order_id": "ORD-1001", "amount": 40})
    assert (outcome.status, outcome.error_code) == ("error", code)
    assert outcome.result is None


def test_a_well_formed_answer_and_an_error_answer(run: Any) -> None:
    with misbehaving_server(respond("200 OK", b'{"result": {"refund_id": "RF-1"}}')) as url:
        ok = ToolClient(base_url=url, tenant="demo-co").call(run, "refund_payment", {"order_id": "ORD-1"})
    assert (ok.status, ok.result, ok.http_status) == ("ok", {"refund_id": "RF-1"}, 200)
    body = b'{"error": {"code": "RATE_LIMITED", "message": "slow down"}}'

    def limited(conn: socket.socket) -> None:
        head = (
            "HTTP/1.1 429 Too Many Requests\r\nContent-Type: application/json\r\nRetry-After: 2\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        )
        conn.sendall(head.encode() + body)

    with misbehaving_server(limited) as url:
        err = ToolClient(base_url=url, tenant="demo-co").call(run, "refund_payment", {"order_id": "ORD-1"})
    assert (err.status, err.error_code, err.retry_after, err.http_status) == (
        "rate_limited",
        "RATE_LIMITED",
        2.0,
        429,
    )


def test_knowledge_base_search_survives_broken_answers(run: Any) -> None:
    for reply in (
        respond("200 OK", b'{"documents": [{"id": "a", "text": "x"}, 3]}'),
        respond("200 OK", b'{"documents": [{"id": "a"'),
        respond("200 OK", b'{"documents": []}', length=300),
        respond("200 OK", b'"just a string"'),
        drop,
    ):
        with misbehaving_server(reply) as url:
            docs = ToolClient(base_url=url, tenant="demo-co", timeout_s=2.0).search_kb(run, "refund policy")
        assert docs in ([], [{"id": "a", "text": "x"}])
