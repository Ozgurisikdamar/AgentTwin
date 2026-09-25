"""Outbound HTTP of the simulation service: the control plane (agent version
lookup), the trace service (verified outcomes) and the agents under test.

Every client ignores proxy environment variables (``trust_env=False``): these
are service-to-service calls inside the deployment, and the agent endpoints
come from operator configuration only.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

import httpx

from agenttwin_core.auth import Principal, TokenService, service_principal
from agenttwin_core.logx import request_id_var

__all__ = [
    "AgentCall",
    "AgentCallKind",
    "AgentClient",
    "ControlPlaneClient",
    "OutcomeResult",
    "TraceServiceClient",
    "UpstreamError",
]

SERVICE = "simulation-service"
MAX_AGENT_RESPONSE = 1 << 20


class UpstreamError(Exception):
    """A dependency did not answer usefully (unreachable, 5xx, bad body)."""


def _headers(tokens: TokenService, p: Principal, audience: str) -> dict[str, str]:
    rid = request_id_var.get() or ""
    headers = {"Authorization": "Bearer " + tokens.mint(p, audience, rid), "Accept": "application/json"}
    if rid:
        headers["X-Request-Id"] = rid
    return headers


class ControlPlaneClient:
    def __init__(
        self,
        base_url: str,
        tokens: TokenService,
        *,
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.tokens = tokens
        self._client = httpx.AsyncClient(timeout=timeout_s, trust_env=False, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    async def agent_version(self, org: str, project: str, agent: str, version: str) -> dict[str, Any] | None:
        """The registered version with its tools, or None when it does not exist."""
        p = service_principal(SERVICE, org, project)
        try:
            resp = await self._client.get(
                self.base_url + "/internal/v1/agent-versions",
                params={"project_id": project, "agent": agent, "version": version},
                headers=_headers(self.tokens, p, "control-plane"),
            )
        except httpx.HTTPError as err:
            raise UpstreamError(f"control plane unreachable: {type(err).__name__}") from None
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise UpstreamError(f"control plane answered {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            raise UpstreamError("control plane answered invalid JSON") from None
        if not isinstance(body, dict):
            raise UpstreamError("control plane answered an unexpected body")
        return body


OutcomeResult = Literal["posted", "retry", "failed"]


class TraceServiceClient:
    def __init__(
        self,
        base_url: str,
        tokens: TokenService,
        *,
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.tokens = tokens
        self._client = httpx.AsyncClient(timeout=timeout_s, trust_env=False, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    async def record_outcome(
        self, org: str, project: str, trace_id: str, body: dict[str, Any]
    ) -> tuple[OutcomeResult, str]:
        """Posts a verified outcome. A trace the service has not ingested yet
        (404) and transient failures are retried by the caller."""
        p = service_principal(SERVICE, org, project)
        try:
            resp = await self._client.post(
                f"{self.base_url}/api/v1/traces/{quote(trace_id, safe='')}/outcome",
                params={"project_id": project},
                json=body,
                headers=_headers(self.tokens, p, "trace-service"),
            )
        except httpx.HTTPError as err:
            return "retry", f"trace service unreachable ({type(err).__name__})"
        if resp.status_code == 200:
            return "posted", "ok"
        if resp.status_code == 404:
            return "retry", "the trace is not ingested yet"
        if resp.status_code == 429 or resp.status_code >= 500:
            return "retry", f"trace service answered {resp.status_code}"
        return "failed", f"trace service rejected the outcome ({resp.status_code}): {resp.text[:300]}"


AgentCallKind = Literal["ok", "timeout", "unreachable", "http_error", "invalid_response"]


@dataclass
class AgentCall:
    """The result of one ``POST /run`` to an agent under test."""

    kind: AgentCallKind
    status_code: int | None
    body: dict[str, Any] | None
    error: str | None
    elapsed_ms: float

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


class AgentClient:
    """Calls agents through the adapter contract (ADR-0011)."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(trust_env=False, transport=transport, follow_redirects=False)

    async def close(self) -> None:
        await self._client.aclose()

    async def run(self, url: str, token: str | None, body: dict[str, Any], timeout_s: float) -> AgentCall:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        start = time.perf_counter()

        def done(
            kind: AgentCallKind, status: int | None, parsed: dict[str, Any] | None, error: str | None
        ) -> AgentCall:
            return AgentCall(kind, status, parsed, error, round((time.perf_counter() - start) * 1000, 2))

        try:
            resp = await self._client.post(
                url.rstrip("/") + "/run",
                content=json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                headers=headers,
                timeout=httpx.Timeout(timeout_s, connect=min(10.0, timeout_s)),
            )
        except httpx.TimeoutException:
            return done("timeout", None, None, f"the agent did not answer within {timeout_s:g}s")
        except httpx.HTTPError as err:
            return done("unreachable", None, None, f"the agent is unreachable ({type(err).__name__})")
        if len(resp.content) > MAX_AGENT_RESPONSE:
            return done("invalid_response", resp.status_code, None, "the agent response is too large")
        try:
            parsed = resp.json()
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict):
            return done(
                "invalid_response" if resp.status_code == 200 else "http_error",
                resp.status_code,
                None,
                f"the agent answered {resp.status_code} without a JSON object",
            )
        if resp.status_code != 200:
            problem = parsed.get("error")
            detail = ""
            if isinstance(problem, dict):
                detail = f": {problem.get('code')} {str(problem.get('message') or '')[:300]}".rstrip()
            return done(
                "http_error", resp.status_code, parsed, f"the agent answered {resp.status_code}{detail}"
            )
        return done("ok", 200, parsed, None)
