"""Outbound HTTP of the evaluation service: the simulation service (scenarios,
the pair of runs, their cases) and the trace service (what each case's model
calls cost). Both are called directly on the internal network with an
internal token of the evaluation service acting for one project (ADR-0009);
proxy environment variables are ignored (``trust_env=False``)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from agenttwin_core.auth import TokenService, service_principal
from agenttwin_core.logx import request_id_var

__all__ = [
    "SERVICE",
    "PairRefused",
    "SimulationClient",
    "TraceClient",
    "TraceUsage",
    "UpstreamError",
]

SERVICE = "evaluation-service"
_PAGE = 200
MAX_PAGES = 50


class UpstreamError(Exception):
    """A dependency did not answer usefully (unreachable, 5xx, bad body)."""


class PairRefused(Exception):
    """The simulation service refused the pair (a 4xx with its error code)."""

    def __init__(self, status: int, code: str, message: str, details: Mapping[str, Any] | None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = dict(details or {})

    def describe(self) -> str:
        """The refusal in one line, naming what it is about: the side, and
        the scenarios or scenario versions that could not be found (at most
        ten, then how many more)."""
        text = f"{self.code}: {self}"
        side = self.details.get("side")
        if side:
            text += f" ({side})"
        for key, label in (("missing", "missing scenarios"), ("missing_versions", "missing versions")):
            missing = self.details.get(key)
            if isinstance(missing, list) and missing:
                shown = ", ".join(str(m) for m in missing[:10])
                more = f" and {len(missing) - 10} more" if len(missing) > 10 else ""
                text += f" [{label}: {shown}{more}]"
        return text


class _Client:
    audience = ""

    def __init__(
        self,
        base_url: str,
        tokens: TokenService,
        *,
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.tokens = tokens
        self._client = httpx.AsyncClient(timeout=timeout_s, trust_env=False, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    def _headers(self, org: str, project: str) -> dict[str, str]:
        rid = request_id_var.get() or ""
        p = service_principal(SERVICE, org, project)
        headers = {
            "Authorization": "Bearer " + self.tokens.mint(p, self.audience, rid),
            "Accept": "application/json",
        }
        if rid:
            headers["X-Request-Id"] = rid
        return headers

    async def _send(
        self,
        method: str,
        org: str,
        project: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
    ) -> httpx.Response:
        try:
            return await self._client.request(
                method,
                self.base_url + path,
                params=params,
                json=body,
                headers=self._headers(org, project),
            )
        except httpx.HTTPError as err:
            raise UpstreamError(f"{self.audience} unreachable: {type(err).__name__}") from None

    @staticmethod
    def _json(resp: httpx.Response, what: str) -> Any:
        try:
            return resp.json()
        except ValueError:
            raise UpstreamError(f"{what}: the answer is not JSON") from None


class SimulationClient(_Client):
    audience = "simulation-service"

    async def scenarios(self, org: str, project: str) -> list[dict[str, Any]]:
        """Every scenario of the project that is not archived."""
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {"project_id": project, "limit": _PAGE}
            if cursor:
                params["cursor"] = cursor
            resp = await self._send("GET", org, project, "/api/v1/scenarios", params=params)
            if resp.status_code != 200:
                raise UpstreamError(f"simulation service answered {resp.status_code} listing scenarios")
            page = self._json(resp, "scenario list")
            out.extend(page.get("items") or [])
            cursor = page.get("next_cursor")
            if not cursor:
                return out
        raise UpstreamError("the scenario list did not end")

    async def start_pair(self, org: str, project: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """Both runs of an evaluation (ADR-0023); a repeat answers the same pair."""
        resp = await self._send("POST", org, project, "/internal/v1/simulation-pairs", body=dict(body))
        if resp.status_code in (200, 201):
            pair: dict[str, Any] = self._json(resp, "simulation pair")
            return pair
        if 400 <= resp.status_code < 500 and resp.status_code not in (401, 403, 408, 429):
            err = (self._json(resp, "pair refusal") or {}).get("error") or {}
            raise PairRefused(
                resp.status_code,
                str(err.get("code") or "REFUSED"),
                str(err.get("message") or f"HTTP {resp.status_code}"),
                err.get("details") if isinstance(err.get("details"), Mapping) else None,
            )
        raise UpstreamError(f"simulation service answered {resp.status_code} to the pair request")

    async def run(self, org: str, project: str, run_id: str) -> dict[str, Any]:
        resp = await self._send("GET", org, project, f"/api/v1/simulations/{quote(run_id, safe='')}")
        if resp.status_code != 200:
            raise UpstreamError(f"simulation service answered {resp.status_code} for run {run_id}")
        run: dict[str, Any] = self._json(resp, "simulation run")
        return run

    async def case(self, org: str, project: str, run_id: str, case_id: str) -> dict[str, Any]:
        path = f"/api/v1/simulations/{quote(run_id, safe='')}/cases/{quote(case_id, safe='')}"
        resp = await self._send("GET", org, project, path)
        if resp.status_code != 200:
            raise UpstreamError(f"simulation service answered {resp.status_code} for case {case_id}")
        case: dict[str, Any] = self._json(resp, "simulation case")
        return case

    async def cancel(self, org: str, project: str, run_id: str) -> None:
        resp = await self._send("POST", org, project, f"/api/v1/simulations/{quote(run_id, safe='')}/cancel")
        if resp.status_code not in (200, 202, 404):
            raise UpstreamError(f"simulation service answered {resp.status_code} cancelling run {run_id}")


class TraceUsage(dict[str, Any]):
    """Tokens and cost of one trace, when the trace says (``None`` = unknown)."""


class TraceClient(_Client):
    audience = "trace-service"

    async def usage_of_run(self, org: str, project: str, simulation_run_id: str) -> dict[str, TraceUsage]:
        """trace id -> usage, for the traces of one simulation run. A trace
        that is not finalized yet, or that recorded no model call, has an
        unknown usage: it is never counted as zero."""
        out: dict[str, TraceUsage] = {}
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {
                "project_id": project,
                "simulation_run_id": simulation_run_id,
                "limit": _PAGE,
            }
            if cursor:
                params["cursor"] = cursor
            resp = await self._send("GET", org, project, "/api/v1/traces", params=params)
            if resp.status_code != 200:
                raise UpstreamError(f"trace service answered {resp.status_code} listing traces")
            page = self._json(resp, "trace list")
            for t in page.get("items") or []:
                known = bool(t.get("finalized")) and int(t.get("model_call_count") or 0) > 0
                tokens = int(t.get("input_tokens") or 0) + int(t.get("output_tokens") or 0)
                cost = t.get("cost_usd")
                out[str(t.get("trace_id"))] = TraceUsage(
                    tokens=tokens if known else None,
                    cost_usd=float(cost) if known and isinstance(cost, int | float) else None,
                )
            cursor = page.get("next_cursor")
            if not cursor:
                return out
        raise UpstreamError("the trace list did not end")
