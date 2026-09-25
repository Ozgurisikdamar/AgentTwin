"""A client of the runtime gateway (ADR-0033): tool calls decided by policy.

An agent contained by AgentTwin calls its tools through the gateway instead
of directly: ``POST {url}/gateway/v1/tools/{tool}`` with the tool's own JSON
arguments. The project's active policies allow the call (it is forwarded and
the tool's answer comes back), deny it, or hold it for a person's approval.
An approval is bound to the exact action: the same agent, tool and arguments.
Once a person approves, the caller claims a short-lived token and repeats the
call with it and with the same idempotency key; the gateway forwards it once.

::

    gateway = Gateway("https://agenttwin.example.com", api_key,
                      agent="support-refund-agent", agent_version="1.3.1")
    answer = gateway.call_approved(
        "refund_payment", {"order_id": "ORD-1003", "amount": 150},
        idempotency_key="refund-ORD-1003", wait_s=300,
    )
    if answer.refusal:        # denied, still waiting, or the tool failed
        ...
    else:
        result = answer.json()

Standard library only. The API key is never included in errors or reprs.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from email.message import Message
from typing import Any

__all__ = [
    "GATEWAY_CODES",
    "ApprovalToken",
    "Decision",
    "Gateway",
    "GatewayError",
    "Refusal",
    "ToolResponse",
]

_MAX_RESPONSE_BYTES = 32 << 20

#: Error codes of an invocation that come from the platform (the gateway or
#: the edge in front of it), not from the tool: the gateway refused the call,
#: or could not get the tool's answer. The tool's own errors pass through as
#: its status and body. (A tool that reuses one of these codes for its own
#: errors is reported as refused.)
GATEWAY_CODES = frozenset(
    {
        "APPROVAL_EXPIRED",
        "APPROVAL_MISMATCH",
        "APPROVAL_NOT_APPROVED",
        "APPROVAL_REQUIRED",
        "APPROVAL_TOKEN_EXPIRED",
        "APPROVAL_TOKEN_INVALID",
        "APPROVAL_USED",
        "EGRESS_BLOCKED",
        "FORBIDDEN",
        "GATEWAY_UNAVAILABLE",
        "IDEMPOTENCY_IN_PROGRESS",
        "IDEMPOTENCY_KEY_REQUIRED",
        "IDEMPOTENCY_KEY_REUSED",
        "INTERNAL",
        "INVALID_ARGUMENTS",
        "INVALID_CONTEXT",
        "INVALID_PARAMETER",
        "NOT_FOUND",
        "PAYLOAD_TOO_LARGE",
        "POLICY_DENIED",
        "PROJECT_REQUIRED",
        "RATE_LIMITED",
        "SERVICE_NOT_CONFIGURED",
        "TOOL_NOT_REGISTERED",
        "TOOL_PROTOCOL_ERROR",
        "TOOL_RESPONSE_TOO_LARGE",
        "TOOL_TIMEOUT",
        "TOOL_UNAVAILABLE",
        "UNAUTHENTICATED",
        "UPSTREAM_UNAVAILABLE",
    }
)

#: Approval states that will not change any more.
_CLOSED = frozenset({"APPROVED", "DENIED", "EXPIRED", "USED"})


@dataclass
class GatewayError(Exception):
    """The gateway could not be reached, or refused a request that is not a
    tool call (reading an approval, claiming its token). ``status`` 0 means no
    answer: a tool call may then have happened — repeat it with the same
    idempotency key, the gateway deduplicates."""

    status: int
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.status} {self.code}: {self.message}"


@dataclass(frozen=True)
class Decision:
    """The gateway's decision on a call, from its response headers."""

    effect: str | None = None  # allow | allow_with_limits | require_approval | deny
    id: str | None = None
    policy: str | None = None
    policy_version: str | None = None
    rule: str | None = None
    replayed: bool = False

    @classmethod
    def from_headers(cls, headers: Message | Mapping[str, str]) -> Decision:
        def get(name: str) -> str | None:
            value = headers.get(name)
            return str(value) if value else None

        return cls(
            effect=get("X-AgentTwin-Decision"),
            id=get("X-AgentTwin-Decision-Id"),
            policy=get("X-AgentTwin-Policy"),
            policy_version=get("X-AgentTwin-Policy-Version"),
            rule=get("X-AgentTwin-Policy-Rule"),
            replayed=(get("Idempotent-Replayed") or "").lower() == "true",
        )


@dataclass(frozen=True)
class Refusal:
    """Why the platform did not return the tool's answer."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def approval_id(self) -> str | None:
        value = self.details.get("approval_id")
        return value if isinstance(value, str) else None


@dataclass(frozen=True)
class ToolResponse:
    """One answer of ``POST /gateway/v1/tools/{tool}``.

    ``refusal`` is set when the platform refused the call or could not get
    the tool's answer; otherwise ``status`` and ``body`` are the tool's own
    (a tool may answer an error too). ``approval`` is the approval request as
    last seen by :meth:`Gateway.call_approved`.
    """

    status: int
    body: bytes
    content_type: str
    decision: Decision
    refusal: Refusal | None = None
    approval: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.refusal is None and 200 <= self.status < 300

    def json(self) -> Any:
        """The body as JSON (``None`` when empty)."""
        return json.loads(self.body) if self.body else None


@dataclass(frozen=True)
class ApprovalToken:
    approval_id: str
    token: str
    expires_at: str

    def __repr__(self) -> str:  # never print the token
        return f"ApprovalToken(approval_id={self.approval_id!r}, expires_at={self.expires_at!r})"


class Gateway:
    """Client of the runtime gateway behind the control plane at ``url``
    (``/gateway/v1`` is added per call). ``agent``, ``agent_version`` and
    ``environment`` describe the caller on every call; ``project_id`` is
    needed only when the API key reaches more than one project."""

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        agent: str | None = None,
        agent_version: str | None = None,
        environment: str | None = None,
        project_id: str | None = None,
        timeout_s: float = 70.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("url must be an http(s) URL")
        if not api_key:
            raise ValueError("api_key is required")
        self.url = url.rstrip("/")
        self._api_key = api_key
        self.agent = agent
        self.agent_version = agent_version
        self.environment = environment
        self.project_id = project_id
        self.timeout_s = timeout_s
        self._sleep = sleep
        self._clock = clock

    def __repr__(self) -> str:  # never print the key
        return f"Gateway(url={self.url!r}, agent={self.agent!r})"

    # ------------------------------------------------------------ tool calls
    def call(
        self,
        tool: str,
        args: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        traceparent: str | None = None,
        approval_token: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> ToolResponse:
        """Calls ``tool`` through the gateway. ``headers`` are the tool's own
        request headers (forwarded only when its endpoint lists them).
        Raises :class:`GatewayError` (status 0) when no answer arrives."""
        hdrs = dict(headers or {})
        hdrs.update(self._context())
        hdrs["Content-Type"] = "application/json"
        if idempotency_key:
            hdrs["Idempotency-Key"] = idempotency_key
        if traceparent:
            hdrs["traceparent"] = traceparent
        if approval_token:
            hdrs["X-AgentTwin-Approval-Token"] = approval_token
        path = f"/gateway/v1/tools/{urllib.parse.quote(tool, safe='')}"
        status, body, resp_headers = self._send("POST", path, json.dumps(dict(args)).encode("utf-8"), hdrs)
        return ToolResponse(
            status=status,
            body=body,
            content_type=str(resp_headers.get("Content-Type") or ""),
            decision=Decision.from_headers(resp_headers),
            refusal=_refusal(body),
        )

    def approval(self, approval_id: str) -> dict[str, Any]:
        """The approval request as the gateway sees it now (its ``status``:
        PENDING, APPROVED, DENIED, EXPIRED or USED)."""
        return self._json("GET", f"/gateway/v1/approvals/{_quote(approval_id)}")

    def claim_token(self, approval_id: str) -> ApprovalToken:
        """The token of an approved request (for the caller that asked for
        it). A new token voids the previous one. Raises GatewayError with
        ``APPROVAL_PENDING``, ``APPROVAL_DENIED``, ``APPROVAL_EXPIRED``,
        ``APPROVAL_USED`` or ``NOT_REQUESTER`` otherwise."""
        out = self._json("POST", f"/gateway/v1/approvals/{_quote(approval_id)}/token")
        return ApprovalToken(out["approval_id"], out["token"], out["expires_at"])

    def wait_for_approval(
        self, approval_id: str, *, timeout_s: float, interval_s: float = 1.0
    ) -> dict[str, Any]:
        """Polls the request until a person decides (or it expires), at most
        ``timeout_s``; returns its last state (still PENDING on timeout)."""
        deadline = self._clock() + max(timeout_s, 0.0)
        while True:
            state = self.approval(approval_id)
            if state.get("status") in _CLOSED or self._clock() >= deadline:
                return state
            self._sleep(max(min(interval_s, deadline - self._clock()), 0.0))

    def call_approved(
        self,
        tool: str,
        args: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        traceparent: str | None = None,
        headers: Mapping[str, str] | None = None,
        wait_s: float = 0.0,
        interval_s: float = 1.0,
        on_response: Callable[[ToolResponse], None] | None = None,
    ) -> ToolResponse:
        """Calls ``tool``; when a person must approve it, waits up to
        ``wait_s`` for the decision, claims the token and repeats the same
        call (same arguments and idempotency key) with it.

        Returns the last answer, with the approval as last seen: when nobody
        decided in time, the ``APPROVAL_REQUIRED`` refusal (the request stays
        open; calling again later with the same arguments finds it). Every
        answer is also passed to ``on_response`` (to record each decision).
        """

        def call(token: str | None = None) -> ToolResponse:
            r = self.call(
                tool,
                args,
                idempotency_key=idempotency_key,
                traceparent=traceparent,
                approval_token=token,
                headers=headers,
            )
            if on_response is not None:
                on_response(r)
            return r

        first = call()
        if (
            first.refusal is None
            or first.refusal.code != "APPROVAL_REQUIRED"
            or not first.refusal.approval_id
        ):
            return first
        approval_id = first.refusal.approval_id
        state = self.wait_for_approval(approval_id, timeout_s=wait_s, interval_s=interval_s)
        if state.get("status") != "APPROVED":
            return _with_approval(first, state)
        try:
            token = self.claim_token(approval_id)
        except GatewayError as err:
            if err.status == 0:
                raise
            return _with_approval(first, {**state, "claim_refused": err.code})
        return _with_approval(call(token.token), state)

    # ------------------------------------------------------------ transport
    def _context(self) -> dict[str, str]:
        out = {"X-AgentTwin-Api-Key": self._api_key}
        for name, value in (
            ("X-AgentTwin-Agent", self.agent),
            ("X-AgentTwin-Agent-Version", self.agent_version),
            ("X-AgentTwin-Environment", self.environment),
            ("X-AgentTwin-Project", self.project_id),
        ):
            if value:
                out[name] = value
        return out

    def _json(self, method: str, path: str) -> dict[str, Any]:
        headers = self._context()
        headers["Accept"] = "application/json"
        status, body, _ = self._send(method, path, None, headers)
        if not 200 <= status < 300:
            r = _platform_error(body) or Refusal("HTTP_ERROR", f"HTTP {status}")
            raise GatewayError(status, r.code, r.message, r.details)
        out = json.loads(body or b"{}")
        if not isinstance(out, dict):
            raise GatewayError(status, "MALFORMED_RESPONSE", "The gateway answered with a non-object body.")
        return out

    def _send(
        self, method: str, path: str, data: bytes | None, headers: Mapping[str, str]
    ) -> tuple[int, bytes, Message]:
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=dict(headers))  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310 - scheme validated
                return resp.status, _read(resp), resp.headers
        except urllib.error.HTTPError as err:
            return err.code, _read(err), err.headers
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
            reason = getattr(err, "reason", err)
            raise GatewayError(0, "UNAVAILABLE", f"{self.url} did not answer: {reason}") from None


def _read(resp: Any) -> bytes:
    raw: bytes = resp.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise GatewayError(0, "RESPONSE_TOO_LARGE", "The response exceeded the client limit.")
    return raw


def _platform_error(body: bytes) -> Refusal | None:
    """The error in ``body`` when it has the platform's shape."""
    try:
        parsed = json.loads(body) if body else None
    except ValueError:
        return None
    err = parsed.get("error") if isinstance(parsed, dict) else None
    if not isinstance(err, dict) or not isinstance(err.get("code"), str):
        return None
    details = err.get("details")
    return Refusal(err["code"], str(err.get("message", "")), details if isinstance(details, dict) else {})


def _refusal(body: bytes) -> Refusal | None:
    """The platform's refusal of a tool call in ``body``, if it is one (a
    tool's own error in the same shape is the tool's answer)."""
    r = _platform_error(body)
    return r if r is not None and r.code in GATEWAY_CODES else None


def _with_approval(r: ToolResponse, approval: dict[str, Any]) -> ToolResponse:
    return ToolResponse(r.status, r.body, r.content_type, r.decision, r.refusal, approval)


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")
