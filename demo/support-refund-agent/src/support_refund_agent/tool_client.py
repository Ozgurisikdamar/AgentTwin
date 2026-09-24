"""HTTP client for the tool contract; every call is traced as a tool span.

The agent never talks to tools directly: it calls this client, which knows
the tools base URL of the current run (Demo Co in production, a tool twin in
simulations, or the runtime gateway when containment is enabled).
"""

from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agenttwin import AgentRun
from agenttwin.tracing import ToolResultStatus

__all__ = ["ToolClient", "ToolOutcome"]


@dataclass
class ToolOutcome:
    """What the agent (and the planner) sees of one tool call."""

    status: ToolResultStatus
    result: Any = None
    error_code: str | None = None
    message: str | None = None
    http_status: int | None = None
    retry_after: float | None = None

    def to_message(self) -> str:
        body: dict[str, Any] = {"status": self.status}
        if self.result is not None:
            body["result"] = self.result
        if self.error_code:
            body["error"] = {"code": self.error_code, "message": self.message}
        if self.retry_after is not None:
            body["retry_after"] = self.retry_after
        return json.dumps(body, sort_keys=True)

    @staticmethod
    def from_message(content: str) -> ToolOutcome:
        body = json.loads(content)
        err = body.get("error") or {}
        return ToolOutcome(
            status=body["status"],
            result=body.get("result"),
            error_code=err.get("code"),
            message=err.get("message"),
            retry_after=body.get("retry_after"),
        )


def _status_for(http_status: int) -> ToolResultStatus:
    if http_status == 429:
        return "rate_limited"
    if http_status in (401, 403):
        return "denied"
    if http_status in (400, 409, 422):
        return "invalid"
    if http_status in (408, 504):
        return "timeout"
    return "error"


@dataclass
class ToolClient:
    base_url: str
    tenant: str
    risks: Mapping[str, str] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout_s: float = 1.5

    def call(
        self,
        run: AgentRun,
        name: str,
        args: dict[str, Any],
        *,
        attempt: int = 1,
        call_id: str | None = None,
    ) -> ToolOutcome:
        idem = args.get("idempotency_key") if isinstance(args.get("idempotency_key"), str) else None
        with run.tool_call(
            name,
            args=args,
            risk=self.risks.get(name),
            idempotency_key=idem,
            attempt=attempt,
            call_id=call_id,
        ) as span:
            outcome = self._post(f"/tools/{urllib.parse.quote(name)}", args)
            if outcome.status == "ok":
                span.set_result(outcome.result)
            else:
                span.set_error(outcome.status, outcome.error_code, http_status=outcome.http_status)
            return outcome

    def search_kb(self, run: AgentRun, query: str, limit: int = 3) -> list[dict[str, Any]]:
        with run.retrieval("support-kb", query=query) as span:
            query_string = urllib.parse.urlencode({"q": query, "limit": limit})
            url = f"{self.base_url.rstrip('/')}/kb/search?{query_string}"
            req = urllib.request.Request(url, headers=dict(self.headers), method="GET")  # noqa: S310
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310
                    found = json.loads(resp.read()).get("documents", [])
                docs: list[dict[str, Any]] = [d for d in found if isinstance(d, dict)]
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, AttributeError, TypeError):
                docs = []
            except http.client.HTTPException:  # a cut-off or garbled response
                docs = []
            span.set_documents(docs)
            return docs

    def _post(self, path: str, args: dict[str, Any]) -> ToolOutcome:
        url = self.base_url.rstrip("/") + path
        if not url.startswith(("http://", "https://")):
            raise ValueError("tools base URL must be http(s)")
        headers = {"Content-Type": "application/json", "X-AgentTwin-Tenant": self.tenant, **self.headers}
        req = urllib.request.Request(  # noqa: S310 - scheme checked above
            url, data=json.dumps(args).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310
                raw, http_status = resp.read(), resp.status
        except urllib.error.HTTPError as err:
            code, message = "HTTP_ERROR", str(err.reason)
            try:
                detail = json.loads(err.read() or b"{}").get("error", {})
                code, message = detail.get("code", code), detail.get("message", message)
            except (ValueError, AttributeError, OSError, http.client.HTTPException):
                pass
            retry_after = None
            if err.headers and err.headers.get("Retry-After"):
                try:
                    retry_after = float(err.headers["Retry-After"])
                except ValueError:
                    retry_after = None
            return ToolOutcome(
                status=_status_for(err.code),
                error_code=code,
                message=message,
                http_status=err.code,
                retry_after=retry_after,
            )
        except TimeoutError:
            return ToolOutcome(
                status="timeout", error_code="TIMEOUT", message="The tool did not answer in time."
            )
        except urllib.error.URLError as err:
            if isinstance(err.reason, TimeoutError | socket.timeout):
                return ToolOutcome(
                    status="timeout", error_code="TIMEOUT", message="The tool did not answer in time."
                )
            return ToolOutcome(
                status="error", error_code="UNREACHABLE", message="The tool could not be reached."
            )
        except http.client.RemoteDisconnected:
            # The request was sent: the tool may have acted before the drop.
            return ToolOutcome(
                status="error",
                error_code="CONNECTION_DROPPED",
                message="The tool closed the connection without answering.",
            )
        except http.client.HTTPException:
            return ToolOutcome(
                status="error",
                error_code="BROKEN_RESPONSE",
                message="The tool's response was cut off or garbled.",
            )
        except (ConnectionError, OSError):
            return ToolOutcome(
                status="error", error_code="UNREACHABLE", message="The tool could not be reached."
            )
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return ToolOutcome(
                status="error",
                error_code="MALFORMED_RESPONSE",
                message="The tool answered with a body that is not a JSON object.",
                http_status=http_status,
            )
        return ToolOutcome(status="ok", result=body.get("result"), http_status=http_status)
