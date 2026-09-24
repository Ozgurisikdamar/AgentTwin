"""Agent adapter HTTP server (ADR-0011).

``POST /run``      run one conversation; body::

    {"input": "...", "customer_id": "CUS-100", "tenant": "demo-co",
     "agent_version": "1.3.0", "session_id": "...",
     "tools_base_url": "http://<twin or tools>", "tool_headers": {...},
     "run_context": {"source": "simulation", "simulation_run_id": "...", "scenario_id": "..."}}

``GET /versions``  agent versions this deployment can run
``GET /healthz``   liveness

Simulations point ``tools_base_url`` at a tool twin, so every tool call the
agent makes is observed by AgentTwin rather than self-reported.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from support_refund_agent.agent import Agent, RunRequest

__all__ = ["AgentServer"]

log = logging.getLogger("support_refund_agent.server")
MAX_BODY = 64 * 1024


class AgentServer:
    def __init__(
        self, agent: Agent, *, host: str = "127.0.0.1", port: int = 0, token: str | None = None
    ) -> None:
        self.agent = agent
        self.token = token
        server = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:
                log.debug("agent: " + fmt, *args)

            def _send(self, status: int, body: Any) -> None:
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _authorized(self) -> bool:
                if not server.token:
                    return True
                given = self.headers.get("Authorization", "")
                return hmac.compare_digest(given.encode(), f"Bearer {server.token}".encode())

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if path == "/healthz":
                    self._send(200, {"status": "ok"})
                elif path == "/versions":
                    self._send(
                        200, {"agent": "support-refund-agent", "versions": server.agent.manifests.versions}
                    )
                else:
                    self._send(404, {"error": {"code": "NOT_FOUND", "message": "Unknown path."}})

            def do_POST(self) -> None:
                if urlparse(self.path).path != "/run":
                    self._send(404, {"error": {"code": "NOT_FOUND", "message": "Unknown path."}})
                    return
                if not self._authorized():
                    self._send(401, {"error": {"code": "UNAUTHORIZED", "message": "Invalid agent token."}})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > MAX_BODY:
                    self._send(
                        413 if length > MAX_BODY else 400,
                        {"error": {"code": "INVALID_BODY", "message": "Bad body size."}},
                    )
                    return
                try:
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ValueError("body must be an object")
                    req = RunRequest.from_json(body)
                except (ValueError, TypeError) as err:
                    self._send(400, {"error": {"code": "INVALID_REQUEST", "message": str(err)}})
                    return
                try:
                    result = server.agent.run(req)
                except KeyError as err:
                    self._send(404, {"error": {"code": "UNKNOWN_VERSION", "message": str(err).strip("'\"")}})
                    return
                except Exception:
                    log.exception("agent run failed")
                    self._send(
                        500, {"error": {"code": "AGENT_ERROR", "message": "The agent failed unexpectedly."}}
                    )
                    return
                self._send(200, result.to_json())

        self._http = ThreadingHTTPServer((host, port), _Handler)
        self._http.daemon_threads = True

    @property
    def url(self) -> str:
        host, port = self._http.server_address[:2]
        if isinstance(host, bytes | bytearray):
            host = host.decode()
        return f"http://{host}:{port}"

    def start(self) -> AgentServer:
        threading.Thread(target=self._http.serve_forever, name="demo-agent", daemon=True).start()
        return self

    def serve_forever(self) -> None:
        self._http.serve_forever()

    def stop(self) -> None:
        self._http.shutdown()
        self._http.server_close()
