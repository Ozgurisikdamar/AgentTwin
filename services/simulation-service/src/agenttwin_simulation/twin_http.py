"""The twin endpoint agents call during a simulation (the tool contract of
ADR-0011, served by the tool twin instead of the production tools):

``POST /twin/v1/tools/{tool}``  a tool call; the body is the arguments object
``GET  /twin/v1/kb/search``     knowledge-base retrieval (``q``, ``limit``)

The credential is the case's capability token (``Authorization: Bearer``):
random per case, stored only as a SHA-256 hash and revoked when the case
closes, so a late or replayed call cannot touch any state. The case row is
locked while a call executes: the calls of one case are applied serially,
exactly in the order the twin records them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from agenttwin_core import errors
from agenttwin_core.db import Conn, transaction
from agenttwin_core.errors import APIError, DeliberateAbort
from agenttwin_simulation.cases import ScenarioRuntime, scenario_runtime
from agenttwin_simulation.config import SimulationConfig
from agenttwin_simulation.metrics import SimulationMetrics
from agenttwin_simulation.store import Row, Store
from agenttwin_simulation.twin.adapters import AdapterRegistry
from agenttwin_simulation.twin.definition import TwinDefinition, load_twin
from agenttwin_simulation.twin.engine import (
    MAX_ARGUMENT_BYTES,
    CallContext,
    CaseState,
    DeclarativeTwin,
    Reply,
)
from agenttwin_simulation.twin.faults import FaultInjectingTwin
from agenttwin_simulation.twin.kb import MAX_RESULTS, collect_documents, search

__all__ = ["TwinEndpoint", "token_hash"]

TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
MAX_BODY = MAX_ARGUMENT_BYTES + 4096
MAX_QUERY = 1000
# Headers a twin may read (idempotency headers and the like); credentials
# and hop-by-hop headers never reach the engine or its records.
_HIDDEN_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization", "connection", "host"})


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


class _LRU[K, V]:
    def __init__(self, size: int) -> None:
        self.size = size
        self._data: OrderedDict[K, V] = OrderedDict()

    def get(self, key: K) -> V | None:
        value = self._data.get(key)
        if value is not None:
            self._data.move_to_end(key)
        return value

    def put(self, key: K, value: V) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self.size:
            self._data.popitem(last=False)


class _Abort(Response):
    """Ends the exchange without a complete response (fault injection).

    ``partial=None`` drops the connection before any byte is written: an
    invalid header value makes the server abandon the response after marking
    it started, so it closes the connection instead of answering 500.
    Otherwise the status line, headers and half of ``partial`` are sent before
    the connection closes (the declared Content-Length is never reached).
    """

    def __init__(
        self, partial: bytes | None = None, status: int = 200, headers: Mapping[str, str] | None = None
    ):
        super().__init__(status_code=status)
        self._partial = partial
        self._extra = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._partial is None:
            try:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [(b"x-agenttwin-fault", b"dropped\n")],
                    }
                )
            except Exception as err:
                raise DeliberateAbort("dropped connection (fault injection)") from err
            raise DeliberateAbort("dropped connection (fault injection)")
        body = self._partial
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    *self._extra,
                ],
            }
        )
        await send({"type": "http.response.body", "body": body[: max(1, len(body) // 2)], "more_body": True})
        raise DeliberateAbort("partial response (fault injection)")


def _reply_response(reply: Reply) -> Response:
    if reply.transport == "drop":
        return _Abort()
    if reply.transport == "partial":
        return _Abort(reply.payload(), reply.status, reply.headers)
    return Response(
        content=reply.payload(),
        status_code=reply.status,
        headers=dict(reply.headers),
        media_type="application/json",
    )


def _bearer(request: Request) -> bytes:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer ") or len(auth) > 512:
        raise APIError(
            401, "TWIN_CREDENTIAL_REQUIRED", "A simulation case token is required (Authorization: Bearer)."
        )
    return token_hash(auth[7:].strip())


def _reject_constant(value: str) -> Any:
    raise ValueError(f"{value} is not valid JSON")


async def _read_arguments(request: Request) -> dict[str, Any]:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY:
        raise errors.payload_too_large()
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise errors.payload_too_large()
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise errors.invalid("INVALID_JSON", "The tool arguments are not valid JSON.") from None
    if not isinstance(body, dict):
        raise errors.invalid("INVALID_ARGUMENTS", "The tool arguments must be a JSON object.")
    return body


class TwinEndpoint:
    def __init__(
        self,
        store: Store,
        cfg: SimulationConfig,
        *,
        adapters: AdapterRegistry | None = None,
        metrics: SimulationMetrics | None = None,
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.adapters = adapters or AdapterRegistry()
        self.metrics = metrics
        # Twin definitions and scenario versions are immutable rows.
        self._definitions: _LRU[str, TwinDefinition] = _LRU(128)
        self._scenarios: _LRU[str, ScenarioRuntime] = _LRU(512)

    async def definition(self, twin_id: str) -> TwinDefinition:
        cached = self._definitions.get(twin_id)
        if cached is not None:
            return cached
        row = await self.store.twin_by_id(twin_id)
        if row is None:
            raise errors.not_found("The twin of this case no longer exists.")
        definition = load_twin(row["document"], adapters=self.adapters.names())
        self._definitions.put(twin_id, definition)
        return definition

    async def scenario(self, version_id: str) -> ScenarioRuntime:
        cached = self._scenarios.get(version_id)
        if cached is not None:
            return cached
        row = await self.store.scenario_document(version_id)
        if row is None:
            raise errors.not_found("The scenario of this case no longer exists.")
        runtime = scenario_runtime(row["document"])
        self._scenarios.put(version_id, runtime)
        return runtime

    async def _open_case(self, conn: Conn, digest: bytes) -> tuple[Row, CaseState]:
        case = await self.store.lock_case_by_token(conn, digest)
        if case is None:
            raise APIError(
                401, "TWIN_CREDENTIAL_INVALID", "The simulation case token is unknown or the case is closed."
            )
        if case["cancel_requested"]:
            raise errors.conflict("RUN_CANCELLED", "The simulation run was cancelled.")
        if case["status"] != "RUNNING" or case["run_status"] != "RUNNING":
            raise errors.conflict("CASE_CLOSED", "The simulation case is not running.")
        state = CaseState.from_json(case["twin_state"])
        if state.seq >= self.cfg.max_calls_per_case:
            raise errors.conflict(
                "CALL_LIMIT_EXCEEDED",
                f"The case reached the limit of {self.cfg.max_calls_per_case} twin calls.",
            )
        return case, state

    def routes(self, app: FastAPI) -> None:
        @app.post("/twin/v1/tools/{tool}", include_in_schema=False)
        async def call_tool(tool: str, request: Request) -> Response:
            digest = _bearer(request)
            if not TOOL_NAME.match(tool):
                raise errors.invalid("INVALID_TOOL_NAME", "Tool names use letters, digits, '_', '.' and '-'.")
            args = await _read_arguments(request)
            headers = {k.lower(): v for k, v in request.headers.items() if k.lower() not in _HIDDEN_HEADERS}
            async with transaction(self.store.pool) as conn:
                case, state = await self._open_case(conn, digest)
                definition = await self.definition(case["twin_definition_id"])
                runtime = await self.scenario(case["scenario_version_id"])
                base = DeclarativeTwin(definition, state, self.adapters)
                twin = FaultInjectingTwin(base, runtime.rules, int(case["seed"]))
                ctx = CallContext(
                    tenant=case["tenant"],
                    allowed_tools=runtime.allowed_tools,
                    headers=headers,
                    now=runtime.now,
                )
                inv = await twin.invoke(tool, args, ctx)
                steps = [(r.seq, "tool_call", r.tool, r.to_json(), float(r.delay_ms)) for r in inv.records]
                await self.store.save_twin_call(conn, case["id"], base.case.to_json(), steps)
            if self.metrics is not None:
                for r in inv.records:
                    self.metrics.twin_call(r.status, r.fault)
            reply = inv.reply
            delay = min(reply.delay_ms, self.cfg.max_fault_delay_ms)
            if delay > 0:
                # The lock is released: the delay models the dependency, not the twin.
                await asyncio.sleep(delay / 1000)
            return _reply_response(reply)

        @app.get("/twin/v1/kb/search", include_in_schema=False)
        async def kb_search(request: Request) -> JSONResponse:
            digest = _bearer(request)
            query = request.query_params.get("q", "")[:MAX_QUERY]
            raw_limit = request.query_params.get("limit") or "3"
            if not raw_limit.isdigit() or not 1 <= int(raw_limit) <= MAX_RESULTS:
                raise errors.invalid("INVALID_PARAMETER", f"limit must be between 1 and {MAX_RESULTS}.")
            limit = int(raw_limit)
            async with transaction(self.store.pool) as conn:
                case, state = await self._open_case(conn, digest)
                definition = await self.definition(case["twin_definition_id"])
                runtime = await self.scenario(case["scenario_version_id"])
                docs = collect_documents(state.state, definition.retrieval_path, runtime.documents)
                found = search(docs, query, limit)
                state.seq += 1
                record = {
                    "seq": state.seq,
                    "kind": "retrieval",
                    "query": query,
                    "limit": limit,
                    "documents": [{"id": d["id"], "trusted": bool(d.get("trusted", True))} for d in found],
                }
                await self.store.save_twin_call(
                    conn, case["id"], state.to_json(), [(state.seq, "retrieval", None, record, None)]
                )
            if self.metrics is not None:
                self.metrics.twin_call("retrieval", None)
            # What the agent sees carries no trust labels: retrieved content is
            # untrusted input to the agent unless it is designed otherwise.
            return JSONResponse(
                {"documents": [{"id": d["id"], "title": d["title"], "text": d["text"]} for d in found]}
            )
