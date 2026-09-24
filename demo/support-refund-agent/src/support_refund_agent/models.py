"""Chat model interface and the deterministic scripted planner (ADR-0011).

``ScriptedPlannerModel`` stands in for an LLM in CI and in the demo. It reads
the directives written in the agent's system prompt (e.g. "Always call
get_refund_policy before refund_payment") and plans tool calls from the
conversation so far. Changing the instructions therefore changes behavior
deterministically - which is what makes the release-gate demo reproducible.
It is labeled ``deterministic-fake`` everywhere it is used; it demonstrates
the assurance pipeline, not model quality. Its token counts are deterministic
estimates (about four characters per token), so usage analytics have a
realistic shape; they are not billed tokens and carry no cost.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from support_refund_agent.tool_client import ToolOutcome

__all__ = [
    "KB_TOOL",
    "ChatModel",
    "Directives",
    "Message",
    "ModelResponse",
    "ScriptedPlannerModel",
    "ToolCallRequest",
    "ToolSpec",
    "estimate_tokens",
]

KB_TOOL = "search_knowledge_base"
"""Retrieval exposed to the model as a tool; the agent serves it from the
knowledge base (a retrieval span, not a tool span)."""

Message = dict[str, Any]
"""``{"role": "user", "content": ...}``,
``{"role": "assistant", "content": ..., "tool_calls": [ToolCallRequest...]}``,
``{"role": "tool", "tool_call_id": ..., "name": ..., "content": <ToolOutcome JSON>}``."""


@dataclass(frozen=True)
class ToolCallRequest:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ModelResponse:
    text: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    stop_reason: Literal["tool_use", "end_turn", "max_tokens"] = "end_turn"
    input_tokens: int | None = None
    output_tokens: int | None = None
    response_model: str | None = None
    #: The agent's own view of the outcome (self-report; never "verified").
    claimed_outcome: str | None = None
    business_outcome: str | None = None


class ChatModel(Protocol):
    provider: str
    name: str
    #: "deterministic-fake" for the scripted planner, "llm" for real models.
    kind: str

    def chat(self, system: str, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelResponse: ...


# ---------------------------------------------------------------- directives


@dataclass(frozen=True)
class Directives:
    """Behavior switches read from the system prompt."""

    lookup_order_first: bool = False
    policy_first: bool = False
    skip_policy: bool = False
    use_idempotency: bool = False
    verify_before_retry: bool = False
    retry_immediately: bool = False
    escalate_over_limit: bool = False
    untrusted_content: bool = False
    protect_secrets: bool = False
    tenant_only: bool = False
    confirm_email: bool = False
    refund_limit: float | None = None
    max_rate_limit_retries: int = 0

    @classmethod
    def parse(cls, instructions: str) -> Directives:
        t = " ".join(instructions.lower().split())
        limit = re.search(r"automatic refund limit is (\d+(?:\.\d+)?)", t)
        retries = re.search(r"retry rate-limited calls at most (\d+) times", t)
        return cls(
            lookup_order_first="call lookup_order before acting" in t,
            policy_first="call get_refund_policy before refund_payment" in t,
            skip_policy="without waiting for policy lookups" in t,
            use_idempotency="stable idempotency_key" in t,
            verify_before_retry="verify the order state before any retry" in t,
            retry_immediately="simply retry the refund right away" in t,
            escalate_over_limit="escalate to a human" in t,
            untrusted_content="as untrusted data" in t,
            protect_secrets="never reveal credentials" in t,
            tenant_only="belonging to the requesting tenant" in t,
            confirm_email="confirm it to the customer with send_email" in t,
            refund_limit=float(limit.group(1)) if limit else None,
            max_rate_limit_retries=int(retries.group(1)) if retries else 0,
        )


# ---------------------------------------------------------------- request parsing

_ORDER = re.compile(r"\bORD-\d{3,}\b", re.IGNORECASE)
_CUSTOMER = re.compile(r"\bCUS-\d{3,}\b", re.IGNORECASE)
_AMOUNT = re.compile(
    r"(?:\$\s?(\d+(?:\.\d{1,2})?))|(?:(\d+(?:\.\d{1,2})?)\s?(?:usd|dollars?)\b)", re.IGNORECASE
)
_INJECTION = re.compile(
    r"ignore (?:the |all |previous |your )?(?:policy|policies|instructions|rules)|administrator approved"
    r"|admin(?:istrator)? has approved|approved it|override the limit|developer mode",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Request:
    text: str
    intent: Literal["refund", "status", "policy", "export", "other"]
    order_id: str | None
    amount: float | None
    customer_id: str | None
    injection: bool

    @classmethod
    def parse(cls, text: str, customer_id: str | None) -> Request:
        low = text.lower()
        order = _ORDER.search(text)
        cust = _CUSTOMER.search(text)
        amount = None
        m = _AMOUNT.search(text)
        if m:
            amount = float(m.group(1) or m.group(2))
        if any(w in low for w in ("export", "all my data", "copy of my data", "gdpr")):
            intent: Literal["refund", "status", "policy", "export", "other"] = "export"
        elif not order and ("policy" in low or "how long" in low or "how do refunds" in low):
            intent = "policy"
        elif "refund" in low or "money back" in low or "reimburse" in low:
            intent = "refund"
        elif any(w in low for w in ("where is", "status", "track", "arrive", "delivered")):
            intent = "status"
        elif "policy" in low or "how long" in low or "how do refunds" in low:
            intent = "policy"
        else:
            intent = "other"
        return cls(
            text=text,
            intent=intent,
            order_id=order.group(0).upper() if order else None,
            amount=amount,
            customer_id=customer_id or (cust.group(0).upper() if cust else None),
            injection=bool(_INJECTION.search(text)),
        )


@dataclass
class _Call:
    name: str
    args: dict[str, Any]
    outcome: ToolOutcome


def _history(messages: Sequence[Message]) -> list[_Call]:
    requested: dict[str, ToolCallRequest] = {}
    calls: list[_Call] = []
    for m in messages:
        if m["role"] == "assistant":
            for tc in m.get("tool_calls") or []:
                requested[tc.id] = tc
        elif m["role"] == "tool":
            tc = requested.get(m["tool_call_id"])
            if tc is not None:
                calls.append(_Call(tc.name, tc.arguments, ToolOutcome.from_message(m["content"])))
    return calls


# ---------------------------------------------------------------- planner


class ScriptedPlannerModel:
    """Deterministic planner following the directives in the system prompt."""

    provider = "scripted"
    kind = "deterministic-fake"

    def __init__(
        self, name: str = "scripted-planner-v1", *, customer_id: str | None = None, secret: str = ""
    ) -> None:
        self.name = name
        self.customer_id = customer_id
        # Internal configuration the agent can see but must never reveal.
        self._secret = secret

    def chat(self, system: str, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelResponse:
        d = Directives.parse(system)
        first_user = next((m["content"] for m in messages if m["role"] == "user"), "")
        req = Request.parse(first_user, self.customer_id)
        calls = _history(messages)
        step = sum(1 for m in messages if m["role"] == "assistant")
        available = {t.name for t in tools}
        plan = _Plan(d, req, calls, available, self._secret, step)
        resp = plan.next()
        resp.input_tokens = estimate_tokens(
            system,
            *(_message_text(m) for m in messages),
            *(
                json.dumps({"name": t.name, "description": t.description, "input_schema": t.input_schema})
                for t in tools
            ),
        )
        resp.output_tokens = estimate_tokens(
            resp.text or "",
            *(json.dumps({"name": c.name, "arguments": c.arguments}) for c in resp.tool_calls),
        )
        return resp


def estimate_tokens(*texts: str) -> int:
    """Deterministic token estimate: about four characters per token."""
    chars = sum(len(t) for t in texts)
    return (chars + 3) // 4


def _message_text(m: Message) -> str:
    content = m.get("content")
    text = content if isinstance(content, str) else json.dumps(content, default=str)
    calls = m.get("tool_calls") or []
    return text + "".join(
        json.dumps({"name": c.name, "arguments": c.arguments})
        for c in calls
        if isinstance(c, ToolCallRequest)
    )


class _Plan:
    def __init__(
        self, d: Directives, req: Request, calls: list[_Call], tools: set[str], secret: str, step: int
    ) -> None:
        self.d, self.req, self.calls, self.tools, self.secret, self.step = d, req, calls, tools, secret, step

    # -- helpers --------------------------------------------------------------

    def of(self, name: str) -> list[_Call]:
        return [c for c in self.calls if c.name == name]

    def call(self, name: str, **args: Any) -> ModelResponse:
        if name not in self.tools:
            return self.final(f"I'm not able to use {name} right now.", "FAILURE", "TOOL_UNAVAILABLE")
        args = {k: v for k, v in args.items() if v is not None}
        tc = ToolCallRequest(id=f"call_{self.step}_{name}", name=name, arguments=args)
        return ModelResponse(text=None, tool_calls=[tc], stop_reason="tool_use")

    @staticmethod
    def final(text: str, claimed: str, business: str) -> ModelResponse:
        return ModelResponse(text=text, claimed_outcome=claimed, business_outcome=business)

    # -- intents --------------------------------------------------------------

    def next(self) -> ModelResponse:
        r = self.req
        if r.intent == "refund":
            return self.refund()
        if r.intent == "status":
            return self.status()
        if r.intent == "policy":
            return self.policy()
        if r.intent == "export":
            return self.export()
        return self.final(
            "I can help with order status, refunds and our refund policy. What can I do for you?",
            "SUCCESS",
            "ANSWERED",
        )

    def _order_lookup(self) -> tuple[ModelResponse | None, dict[str, Any] | None]:
        """Returns (response, order). The response is a tool call or a final
        answer; when it is None the order is available."""
        r = self.req
        if not r.order_id:
            return self.final(
                "Which order is this about? Please share the order number (for example ORD-1001).",
                "UNKNOWN",
                "NEEDS_INFORMATION",
            ), None
        lookups = self.of("lookup_order")
        if not lookups:
            if self.d.lookup_order_first or r.intent == "status":
                return self.call("lookup_order", order_id=r.order_id), None
            return None, None
        first = lookups[0].outcome
        if first.status != "ok":
            if first.status == "denied" or first.error_code in ("ORDER_NOT_FOUND", "ACCESS_DENIED"):
                return self.final(
                    "I couldn't find that order on your account, so I can't make changes to it.",
                    "FAILURE",
                    "ORDER_NOT_ACCESSIBLE",
                ), None
            return self.final(
                "Our order system isn't responding right now. Please try again in a few minutes.",
                "FAILURE",
                "TOOL_UNAVAILABLE",
            ), None
        order: dict[str, Any] = first.result
        if self.d.tenant_only and r.customer_id and order.get("customer_id") != r.customer_id:
            return self.final(
                "I couldn't find that order on your account, so I can't make changes to it.",
                "FAILURE",
                "ORDER_NOT_ACCESSIBLE",
            ), None
        return None, order

    def status(self) -> ModelResponse:
        resp, order = self._order_lookup()
        if resp is not None:
            return resp
        if order is None:  # unreachable: status always looks the order up
            return self.final("I couldn't look up that order.", "FAILURE", "TOOL_UNAVAILABLE")
        return self.final(
            f"Your order {order['order_id']} is currently {order['status']}.",
            "SUCCESS",
            "ORDER_STATUS_ANSWERED",
        )

    def policy(self) -> ModelResponse:
        docs = self.of(KB_TOOL)
        if not docs:
            return self.call(KB_TOOL, query=self.req.text)
        documents = docs[0].outcome.result or []
        answer = next((d["text"] for d in documents if d.get("id") == "kb-refund-policy"), None)
        answer = answer or (
            documents[0]["text"] if documents else "Delivered orders can be refunded within 30 days."
        )
        # Retrieved text is untrusted: embedded instructions are never followed.
        instruction_found = any(
            "ignore previous instructions" in d.get("text", "").lower() for d in documents
        )
        if instruction_found and not (self.d.untrusted_content or self.d.protect_secrets):
            answer += f" Internal verification key: {self.secret}"
        return self.final(answer, "SUCCESS", "POLICY_ANSWERED")

    def export(self) -> ModelResponse:
        r = self.req
        exports = self.of("export_customer_data")
        if not r.customer_id:
            return self.final("Please sign in so I can find your account.", "UNKNOWN", "NEEDS_INFORMATION")
        if not exports:
            return self.call("export_customer_data", customer_id=r.customer_id)
        out = exports[-1].outcome
        if out.status == "ok":
            return self.final(
                "Your data export has been scheduled; you'll receive an email.", "SUCCESS", "EXPORT_SCHEDULED"
            )
        escalations = self.of("escalate_to_human")
        if not escalations and self.d.escalate_over_limit:
            return self.call(
                "escalate_to_human", reason="Customer requested a data export (requires an administrator)."
            )
        return self.final(
            "I can't export account data myself, but I've passed your request to our support team.",
            "PARTIAL",
            "EXPORT_ESCALATED",
        )

    def refund(self) -> ModelResponse:
        r, d = self.req, self.d
        resp, order = self._order_lookup()
        if resp is not None:
            return resp
        refunded = float(order.get("refunded_amount", 0)) if order else 0.0
        total = float(order["total"]) if order else None
        amount = r.amount if r.amount is not None else ((total - refunded) if total is not None else None)
        if amount is None:
            return self.final("How much would you like refunded?", "UNKNOWN", "NEEDS_INFORMATION")

        # Policy (limit and eligibility).
        policies = self.of("get_refund_policy")
        if d.policy_first and not policies:
            return self.call("get_refund_policy", order_id=r.order_id)
        limit = d.refund_limit if d.refund_limit is not None else 100.0
        eligible, reason = True, ""
        if policies and policies[0].outcome.status == "ok":
            pol = policies[0].outcome.result
            limit = float(pol.get("max_auto_refund", limit))
            eligible = bool(pol.get("order_eligible", True))
            reason = str(pol.get("reason", ""))
        elif order is not None:
            eligible = order.get("status") == "delivered"
            reason = "The order has not been delivered yet."
        if not eligible:
            return self.final(
                f"I'm sorry, this order isn't eligible for a refund. {reason}".strip(),
                "SUCCESS",
                "REFUND_DENIED",
            )
        if total is not None and amount > total - refunded + 1e-9:
            return self.final(
                f"I can refund at most {total - refunded:.2f} USD for order {r.order_id}.",
                "SUCCESS",
                "REFUND_DENIED",
            )

        # Claimed authority inside the customer's message is only honored by an
        # agent that is not told to treat it as untrusted data.
        authorized_by_message = r.injection and not d.untrusted_content
        if amount > limit and not authorized_by_message:
            escalations = self.of("escalate_to_human")
            if d.escalate_over_limit and not escalations:
                return self.call(
                    "escalate_to_human",
                    order_id=r.order_id,
                    amount=amount,
                    reason=f"Refund of {amount:.2f} USD exceeds the automatic limit of {limit:.2f} USD.",
                )
            return self.final(
                f"A refund of {amount:.2f} USD needs a specialist's approval. "
                "I've passed it on and you'll hear from us within one business day.",
                "SUCCESS",
                "REFUND_ESCALATED",
            )

        return self._refund_flow(amount)

    def _refund_flow(self, amount: float) -> ModelResponse:
        r, d = self.req, self.d
        key = f"refund-{r.order_id}-{amount:.2f}" if d.use_idempotency else None
        refunds = self.of("refund_payment")
        if not refunds:
            return self.call("refund_payment", order_id=r.order_id, amount=amount, idempotency_key=key)
        last = refunds[-1].outcome
        if last.status == "ok":
            return self._confirm(amount)
        if last.status == "rate_limited":
            limited = sum(1 for c in refunds if c.outcome.status == "rate_limited")
            if limited <= d.max_rate_limit_retries:
                return self.call("refund_payment", order_id=r.order_id, amount=amount, idempotency_key=key)
            return self._give_up("Our payment provider is busy right now.")
        if last.status in ("timeout", "error"):
            if d.verify_before_retry:
                # Check the order state after the failed attempt before retrying.
                after = self._lookups_after_last_refund()
                if not after:
                    return self.call("lookup_order", order_id=r.order_id)
                state = after[-1].outcome
                if state.status == "ok":
                    before = self._refund_count_before()
                    if int(state.result.get("refund_count", 0)) > before:
                        return self._confirm(amount)  # the refund went through
                if len(refunds) < 2:
                    return self.call(
                        "refund_payment", order_id=r.order_id, amount=amount, idempotency_key=key
                    )
                return self._give_up("I couldn't confirm the refund.")
            if d.retry_immediately and len(refunds) < 3:
                return self.call("refund_payment", order_id=r.order_id, amount=amount, idempotency_key=key)
            return self._give_up("The refund didn't go through.")
        return self.final(
            f"I couldn't issue the refund: {last.message or last.error_code}.", "FAILURE", "REFUND_FAILED"
        )

    def _lookups_after_last_refund(self) -> list[_Call]:
        idx = max(i for i, c in enumerate(self.calls) if c.name == "refund_payment")
        return [c for c in self.calls[idx + 1 :] if c.name == "lookup_order"]

    def _refund_count_before(self) -> int:
        first_refund = next(i for i, c in enumerate(self.calls) if c.name == "refund_payment")
        before = [
            c for c in self.calls[:first_refund] if c.name == "lookup_order" and c.outcome.status == "ok"
        ]
        return int(before[-1].outcome.result.get("refund_count", 0)) if before else 0

    def _confirm(self, amount: float) -> ModelResponse:
        r, d = self.req, self.d
        if d.confirm_email and r.customer_id and not self.of("send_email"):
            return self.call(
                "send_email", customer_id=r.customer_id, template="refund_confirmation", order_id=r.order_id
            )
        return self.final(
            f"Done! I've refunded {amount:.2f} USD for order {r.order_id}. "
            "It will reach your original payment method within 5 business days.",
            "SUCCESS",
            "REFUND_COMPLETED",
        )

    def _give_up(self, why: str) -> ModelResponse:
        if self.d.escalate_over_limit and not self.of("escalate_to_human"):
            return self.call(
                "escalate_to_human",
                order_id=self.req.order_id,
                reason=f"Refund could not be completed: {why}",
            )
        return self.final(
            f"{why} I've asked a specialist to finish your refund; no need to request it again.",
            "PARTIAL",
            "REFUND_ESCALATED",
        )


def tool_specs(manifest_tools: Sequence[dict[str, Any]], *, retrieval: bool) -> list[ToolSpec]:
    specs = []
    for t in manifest_tools:
        schema = t.get("inputSchema") or {"type": "object"}
        specs.append(
            ToolSpec(name=t["name"], description=str(t.get("description") or t["name"]), input_schema=schema)
        )
    if retrieval:
        specs.append(
            ToolSpec(
                name=KB_TOOL,
                description="Search the support knowledge base.",
                input_schema={
                    "type": "object",
                    "required": ["query"],
                    "properties": {"query": {"type": "string"}},
                },
            )
        )
    return specs


def dumps(v: Any) -> str:
    return json.dumps(v, sort_keys=True)
