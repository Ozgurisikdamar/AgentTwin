"""HTTP server exposing Demo Co's tools (the "production" tools).

Tool contract (shared with AgentTwin's tool twins):

``POST /tools/{name}``  JSON arguments; headers ``X-AgentTwin-Tenant`` and
                        optionally ``X-AgentTwin-Caller-Role``.
                        200 ``{"result": ...}``; errors
                        ``{"error": {"code", "message"}}`` with an HTTP status
                        (429 carries ``Retry-After``).
``GET /kb/search?q=``   retrieval: ``{"documents": [...]}``
``GET /healthz``        liveness

Production-like incidents can be injected (``DEMO_TOOLS_FAULTS``), e.g. a
refund that is applied but whose response never reaches the caller.
"""

from __future__ import annotations

import contextlib
import json
import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from support_refund_agent.world import ToolError, World, default_world

__all__ = ["Fault", "ToolsServer", "parse_faults"]

log = logging.getLogger("support_refund_agent.tools")

FAULT_KINDS = (
    "timeout_after_mutation",
    "timeout_before_mutation",
    "rate_limit",
    "server_error",
    "success_lie",
)
MAX_BODY = 64 * 1024


@dataclass(frozen=True)
class Fault:
    tool: str
    kind: str
    probability: float = 1.0
    delay_s: float = 2.5
    #: Apply to at most this many calls of the tool (None = every call).
    times: int | None = None


def parse_faults(spec: str | None) -> list[Fault]:
    """``tool:kind:probability[,...]`` or a JSON list of objects."""
    if not spec:
        return []
    spec = spec.strip()
    if spec.startswith("["):
        return [Fault(**f) for f in json.loads(spec)]
    faults = []
    for part in spec.split(","):
        tool, kind, *rest = part.strip().split(":")
        if kind not in FAULT_KINDS:
            raise ValueError(f"unknown fault kind {kind!r}")
        faults.append(Fault(tool, kind, float(rest[0]) if rest else 1.0))
    return faults


Handler = Callable[[World, str, str, dict[str, Any]], Any]


def _req(args: dict[str, Any], name: str, kind: type | tuple[type, ...] = str) -> Any:
    if name not in args or args[name] is None:
        raise ToolError(422, "MISSING_ARGUMENT", f"{name} is required.")
    value = args[name]
    if not isinstance(value, kind) or isinstance(value, bool):
        raise ToolError(422, "INVALID_ARGUMENT", f"{name} has the wrong type.")
    return value


TOOLS: dict[str, Handler] = {
    "lookup_customer": lambda w, tenant, role, a: w.lookup_customer(tenant, _req(a, "customer_id")),
    "lookup_order": lambda w, tenant, role, a: w.lookup_order(tenant, _req(a, "order_id")),
    "get_refund_policy": lambda w, tenant, role, a: w.get_refund_policy(tenant, a.get("order_id")),
    "refund_payment": lambda w, tenant, role, a: w.refund_payment(
        tenant, _req(a, "order_id"), _req(a, "amount", (int, float)), a.get("idempotency_key")
    ),
    "send_email": lambda w, tenant, role, a: w.send_email(
        tenant, _req(a, "customer_id"), _req(a, "template"), a.get("order_id")
    ),
    "escalate_to_human": lambda w, tenant, role, a: w.escalate_to_human(
        tenant, a.get("order_id"), str(a.get("reason") or ""), a.get("amount")
    ),
    "export_customer_data": lambda w, tenant, role, a: w.export_customer_data(
        tenant, _req(a, "customer_id"), caller_role=role
    ),
}


class ToolsServer:
    def __init__(
        self,
        world: World | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        faults: list[Fault] | None = None,
        seed: int = 7,
        admin_token: str | None = None,
    ) -> None:
        self.world = world or default_world()
        self.faults = faults or []
        self.admin_token = admin_token
        self._rng = random.Random(seed)
        self._rng_lock = threading.Lock()
        self._fired: dict[int, int] = {}
        # Orders created with ``inject_faults: false`` never receive the
        # configured probabilistic faults, so end-to-end tests are deterministic.
        self._fault_free_orders: set[str] = set()
        self.calls: list[dict[str, Any]] = []
        server = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:
                log.debug("tools: " + fmt, *args)

            def _send(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                url = urlparse(self.path)
                if url.path == "/healthz":
                    self._send(200, {"status": "ok"})
                elif url.path == "/kb/search":
                    q = parse_qs(url.query)
                    docs = server.world.search_kb(q.get("q", [""])[0], int(q.get("limit", ["3"])[0]))
                    self._send(200, {"documents": docs})
                elif url.path == "/state" and server._admin(self.headers.get("Authorization")):
                    self._send(200, server.world.snapshot())
                else:
                    self._send(404, {"error": {"code": "NOT_FOUND", "message": "Unknown path."}})

            def do_POST(self) -> None:
                url = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    self._send(413, {"error": {"code": "TOO_LARGE", "message": "Request body too large."}})
                    return
                raw = self.rfile.read(length) if length else b"{}"
                if url.path == "/admin/orders":
                    if not server._admin(self.headers.get("Authorization")):
                        self._send(
                            401, {"error": {"code": "UNAUTHORIZED", "message": "Admin token required."}}
                        )
                        return
                    try:
                        spec = json.loads(raw or b"{}")
                        inject_faults = spec.get("inject_faults", True)
                        if not isinstance(inject_faults, bool):
                            raise TypeError("inject_faults must be a boolean")
                        order = server.world.create_order(
                            str(spec.get("tenant") or "demo-co"),
                            str(spec["customer_id"]),
                            float(spec["total"]),
                            status=str(spec.get("status") or "delivered"),
                            age_days=int(spec.get("age_days") or 2),
                        )
                        if not inject_faults:
                            server.exempt_from_faults(str(order["order_id"]))
                    except (KeyError, ValueError, TypeError):
                        self._send(
                            400,
                            {
                                "error": {
                                    "code": "INVALID_ORDER",
                                    "message": "customer_id and total are required.",
                                }
                            },
                        )
                        return
                    except ToolError as err:
                        self._send(err.status, {"error": {"code": err.code, "message": err.message}})
                        return
                    self._send(201, order)
                    return
                if not url.path.startswith("/tools/"):
                    self._send(404, {"error": {"code": "NOT_FOUND", "message": "Unknown path."}})
                    return
                name = url.path.removeprefix("/tools/")
                try:
                    args = json.loads(raw or b"{}")
                    if not isinstance(args, dict):
                        raise ValueError
                except ValueError:
                    self._send(
                        400, {"error": {"code": "INVALID_JSON", "message": "Body must be a JSON object."}}
                    )
                    return
                tenant = self.headers.get("X-AgentTwin-Tenant") or "demo-co"
                role = self.headers.get("X-AgentTwin-Caller-Role") or "agent"
                status, body, headers, delay = server.invoke(name, tenant, role, args)
                if delay:
                    time.sleep(delay)
                # The caller may have given up (timeout) - that is the point of the fault.
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self._send(status, body, headers)

        self._http = ThreadingHTTPServer((host, port), _Handler)
        self._http.daemon_threads = True
        self._thread: threading.Thread | None = None

    def exempt_from_faults(self, order_id: str) -> None:
        with self._rng_lock:
            self._fault_free_orders.add(order_id)

    def _admin(self, auth: str | None) -> bool:
        return self.admin_token is not None and auth == f"Bearer {self.admin_token}"

    @property
    def url(self) -> str:
        host, port = self._http.server_address[:2]
        if isinstance(host, bytes | bytearray):
            host = host.decode()
        return f"http://{host}:{port}"

    def invoke(
        self, name: str, tenant: str, role: str, args: dict[str, Any]
    ) -> tuple[int, Any, dict[str, str], float]:
        handler = TOOLS.get(name)
        if handler is None:
            return 404, {"error": {"code": "UNKNOWN_TOOL", "message": f"No tool {name}."}}, {}, 0.0
        fault = self._pick_fault(name, args.get("order_id"))
        record = {"tool": name, "args": args, "tenant": tenant, "fault": fault.kind if fault else None}
        self.calls.append(record)
        if fault and fault.kind == "rate_limit":
            return (
                429,
                {"error": {"code": "RATE_LIMITED", "message": "Too many requests."}},
                {"Retry-After": "1"},
                0.0,
            )
        if fault and fault.kind == "server_error":
            return 500, {"error": {"code": "UPSTREAM_ERROR", "message": "Payment provider error."}}, {}, 0.0
        if fault and fault.kind == "timeout_before_mutation":
            return 504, {"error": {"code": "TIMEOUT", "message": "Upstream timeout."}}, {}, fault.delay_s
        try:
            if fault and fault.kind == "success_lie" and name == "refund_payment":
                result = self.world.refund_payment(
                    tenant,
                    _req(args, "order_id"),
                    _req(args, "amount", (int, float)),
                    args.get("idempotency_key"),
                    apply=False,
                )
            else:
                result = handler(self.world, tenant, role, args)
        except ToolError as err:
            headers = {"Retry-After": str(err.retry_after)} if err.retry_after else {}
            return err.status, {"error": {"code": err.code, "message": err.message}}, headers, 0.0
        delay = fault.delay_s if fault and fault.kind == "timeout_after_mutation" else 0.0
        return 200, {"result": result}, {}, delay

    def _pick_fault(self, tool: str, order_id: object = None) -> Fault | None:
        with self._rng_lock:
            if isinstance(order_id, str) and order_id in self._fault_free_orders:
                return None
            for i, f in enumerate(self.faults):
                if f.tool != tool:
                    continue
                if f.times is not None and self._fired.get(i, 0) >= f.times:
                    continue
                if self._rng.random() < f.probability:
                    self._fired[i] = self._fired.get(i, 0) + 1
                    return f
        return None

    def start(self) -> ToolsServer:
        self._thread = threading.Thread(target=self._http.serve_forever, name="demo-tools", daemon=True)
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        self._http.serve_forever()

    def stop(self) -> None:
        self._http.shutdown()
        self._http.server_close()
