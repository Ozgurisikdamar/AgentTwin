"""The container HEALTHCHECK probe: what it answers, and that it stays cheap
(it runs every few seconds in every Python container)."""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agenttwin_core.probe import healthcheck


@pytest.fixture
def local_service() -> Iterator[tuple[int, dict[str, int]]]:
    """A local process answering /health/{ready,live} with the given codes."""
    answers = {"/health/ready": 200, "/health/live": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(answers.get(self.path, 404))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1], answers
    finally:
        server.shutdown()
        server.server_close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
    return port


def test_readiness_by_default_and_liveness_on_request(
    local_service: tuple[int, dict[str, int]], capsys: pytest.CaptureFixture[str]
) -> None:
    port, answers = local_service
    assert healthcheck(str(port), 1, []) == 0
    assert healthcheck(str(port), 1, ["ready"]) == 0
    assert healthcheck(str(port), 1, ["live"]) == 0
    answers["/health/ready"] = 503
    assert healthcheck(str(port), 1, []) == 1
    assert "/health/ready answered 503" in capsys.readouterr().err
    assert healthcheck(str(port), 1, ["live"]) == 0  # alive, just not ready
    answers["/health/live"] = 500
    assert healthcheck(str(port), 1, ["live"]) == 1


def test_the_default_port_is_used_without_PORT(local_service: tuple[int, dict[str, int]]) -> None:
    port, _ = local_service
    assert healthcheck(None, port, []) == 0
    assert healthcheck("", port, []) == 0
    assert healthcheck(str(free_port()), port, []) == 1  # PORT wins


def test_a_process_that_does_not_answer_is_unhealthy(capsys: pytest.CaptureFixture[str]) -> None:
    assert healthcheck(str(free_port()), 1, []) == 1
    assert capsys.readouterr().err.startswith("healthcheck: ")


def test_an_answer_that_is_not_http_is_unhealthy(capsys: pytest.CaptureFixture[str]) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        def garbage() -> None:
            conn, _ = listener.accept()
            with conn:
                conn.recv(1024)
                conn.sendall(b"hello there\r\n")

        t = threading.Thread(target=garbage, daemon=True)
        t.start()
        assert healthcheck(str(port), 1, []) == 1
        t.join(5)
    assert "not an HTTP answer" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("port_env", "args"),
    [("abc", []), ("0", []), ("70000", []), ("8080", ["sideways"]), ("8080", ["live", "ready"])],
)
def test_misuse_is_exit_code_2(port_env: str, args: list[str]) -> None:
    assert healthcheck(port_env, 1, args) == 2


def test_the_probe_imports_nothing_heavy() -> None:
    """What the probe imports is paid on every probe; the service, its web
    framework and database driver, or even urllib.request (ssl, email) cost
    from a quarter of a second to 1.5 CPU seconds a time."""
    heavy = ("fastapi", "psycopg", "uvicorn", "ssl", "urllib.request", "agenttwin_core.service")
    code = f"import sys, agenttwin_core.probe; print(','.join(m for m in {heavy!r} if m in sys.modules))"
    out = subprocess.run(  # noqa: S603 - this interpreter, a fixed program
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    assert out.stdout.strip() == ""
