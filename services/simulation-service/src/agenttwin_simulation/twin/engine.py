"""The declarative stateful tool twin (spec §24, §85).

A twin answers the agent's tool calls from an isolated per-case state. Every
call is recorded *by the twin* - what the agent sent, what it received, whether
the state changed and how - so trajectory evidence cannot be forged by the
agent under test.

The engine is deterministic: the same definition, initial state, calls and
fault rules always produce the same replies, records and final state
(generated ids come from the call sequence, never from a clock or RNG).
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from agenttwin.hashing import canonical_json
from agenttwin_core.compare import RegexTimeout, compare
from agenttwin_core.evaluators import ToolCall
from agenttwin_core.jsonschema_safe import validation_errors
from agenttwin_core.paths import (
    MISSING,
    PathError,
    Segment,
    delete_path,
    diff_state,
    format_path,
    get_path,
    json_equal,
    set_path,
)
from agenttwin_simulation.twin.adapters import AdapterRegistry, AdapterRequest
from agenttwin_simulation.twin.definition import Effect, ErrorSpec, ToolDef, TwinDefinition
from agenttwin_simulation.twin.templates import Env, TemplateError, render, render_path

__all__ = [
    "CallContext",
    "CallRecord",
    "CaseState",
    "DeclarativeTwin",
    "FaultBehavior",
    "Invocation",
    "Reply",
    "ToolTwin",
    "call_status",
    "diff_state",
    "merge_patch",
    "subset_match",
]

Transport = Literal["normal", "drop", "partial", "malformed"]
Mode = Literal["normal", "no_mutation", "first_effect_only", "stale"]
MAX_ARGUMENT_BYTES = 256 * 1024


# ---------------------------------------------------------------- data


@dataclass
class CaseState:
    """Everything a twin knows about one case; persisted between calls."""

    state: dict[str, Any]
    initial: dict[str, Any]
    seq: int = 0
    call_counts: dict[str, int] = field(default_factory=dict)
    # "<tool>\n<key>" -> {"args_hash", "status", "result"}
    idempotency: dict[str, dict[str, Any]] = field(default_factory=dict)
    # tool -> {"status", "body", "headers"}: the last reply (duplicate_response)
    last_replies: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def fresh(cls, initial: Mapping[str, Any]) -> CaseState:
        return cls(state=copy.deepcopy(dict(initial)), initial=copy.deepcopy(dict(initial)))

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "initial": self.initial,
            "seq": self.seq,
            "call_counts": self.call_counts,
            "idempotency": self.idempotency,
            "last_replies": self.last_replies,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> CaseState:
        return cls(
            state=dict(raw["state"]),
            initial=dict(raw["initial"]),
            seq=int(raw.get("seq", 0)),
            call_counts={str(k): int(v) for k, v in (raw.get("call_counts") or {}).items()},
            idempotency=dict(raw.get("idempotency") or {}),
            last_replies=dict(raw.get("last_replies") or {}),
        )


@dataclass(frozen=True)
class CallContext:
    # The case's tenant comes from the scenario, never from the agent's headers.
    tenant: str | None = None
    caller_role: str = "agent"
    # None: every tool of the twin may be called.
    allowed_tools: frozenset[str] | None = None
    headers: Mapping[str, str] = field(default_factory=dict)  # lower-case names
    now: str = ""


@dataclass(frozen=True)
class FaultBehavior:
    type: str
    delay_ms: int | None = None
    retry_after_s: float | None = None
    body: Any = None
    has_body: bool = False
    message: str | None = None


@dataclass
class Reply:
    """What goes back on the wire."""

    status: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    delay_ms: int = 0
    transport: Transport = "normal"

    def payload(self) -> bytes:
        raw = json.dumps(self.body, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8")
        if self.transport == "malformed":
            # Valid HTTP, invalid JSON: the response is cut in half.
            return raw[: max(1, len(raw) // 2)]
        return raw


@dataclass
class CallRecord:
    """One tool call as observed by the twin."""

    seq: int
    call_number: int
    tool: str
    arguments: dict[str, Any]
    http_status: int  # 0: no response reached the caller
    status: str
    error_code: str | None = None
    response: Any = None
    risk: str | None = None
    fault: str | None = None
    mutated: bool = False
    expects_mutation: bool = False
    replayed: bool = False
    effect_key: str | None = None
    cross_tenant: str | None = None
    policy_violation: str | None = None
    redelivered: bool = False
    changes: list[dict[str, Any]] = field(default_factory=list)
    delay_ms: int = 0

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> CallRecord:
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in raw.items() if k in fields})

    def to_tool_call(self, latency_ms: float = 0.0) -> ToolCall:
        return ToolCall.from_record(self.to_json(), latency_ms)


@dataclass
class Invocation:
    reply: Reply
    records: list[CallRecord]


class ToolTwin(Protocol):
    """The twin interface (spec §85)."""

    async def reset(self, initial_state: Mapping[str, Any] | None = None) -> None: ...

    async def invoke(
        self, tool_name: str, arguments: Mapping[str, Any], context: CallContext
    ) -> Invocation: ...

    async def snapshot(self) -> dict[str, Any]: ...


# ---------------------------------------------------------------- helpers


def merge_patch(target: Any, patch: Any) -> Any:
    """JSON Merge Patch (RFC 7396): objects merge recursively, ``null``
    removes a key, anything else replaces."""
    if not isinstance(patch, Mapping):
        return copy.deepcopy(patch)
    out: dict[str, Any] = dict(copy.deepcopy(target)) if isinstance(target, Mapping) else {}
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = merge_patch(out.get(key), value)
    return out


def call_status(http_status: int, transport: Transport = "normal", error_code: str | None = None) -> str:
    if transport == "drop":
        return "dropped"
    if transport == "partial":
        return "partial"
    if transport == "malformed":
        return "malformed"
    if 200 <= http_status < 300:
        return "ok"
    if http_status == 429:
        return "rate_limited"
    if http_status in (401, 403):
        return "denied"
    if http_status == 404:
        return "unknown_tool" if error_code == "UNKNOWN_TOOL" else "not_found"
    if http_status in (408, 504):
        return "timeout"
    if http_status in (400, 409, 422):
        return "invalid"
    return "error"


def _args_hash(args: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(args)).encode("utf-8")).hexdigest()


def _foreign_tenant(value: Any, tenant_key: str, tenant: str, depth: int = 0) -> bool:
    if depth > 32:
        return False
    if isinstance(value, Mapping):
        owner = value.get(tenant_key)
        if isinstance(owner, str) and owner != tenant:
            return True
        return any(_foreign_tenant(v, tenant_key, tenant, depth + 1) for v in value.values())
    if isinstance(value, list):
        return any(_foreign_tenant(v, tenant_key, tenant, depth + 1) for v in value)
    return False


def subset_match(match: Any, value: Any) -> bool:
    if isinstance(match, Mapping):
        return isinstance(value, Mapping) and all(
            k in value and subset_match(v, value[k]) for k, v in match.items()
        )
    return json_equal(match, value)


class _Stop(Exception):
    def __init__(self, status: int, code: str, message: str, **record: Any) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.record = status, code, message, record


@dataclass
class _Outcome:
    status: int
    result: Any = None
    error_code: str | None = None
    message: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    mutated: bool = False
    replayed: bool = False
    changes: list[dict[str, Any]] = field(default_factory=list)
    cross_tenant: str | None = None
    policy_violation: str | None = None
    committed_state: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def reply(self) -> Reply:
        if self.ok:
            return Reply(self.status, {"result": self.result}, dict(self.headers))
        return Reply(
            self.status, {"error": {"code": self.error_code, "message": self.message}}, dict(self.headers)
        )


# ---------------------------------------------------------------- the twin


class DeclarativeTwin:
    """Executes a :class:`TwinDefinition` against a :class:`CaseState`."""

    def __init__(
        self,
        definition: TwinDefinition,
        case: CaseState | None = None,
        adapters: AdapterRegistry | None = None,
    ) -> None:
        self.definition = definition
        self.case = case or CaseState.fresh(definition.initial_state)
        self.adapters = adapters or AdapterRegistry()

    async def reset(self, initial_state: Mapping[str, Any] | None = None) -> None:
        self.case = CaseState.fresh(self.definition.initial_state if initial_state is None else initial_state)

    async def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self.case.state)

    def next_call_number(self, tool: str) -> int:
        return self.case.call_counts.get(tool, 0) + 1

    # -- invoke ---------------------------------------------------------------

    async def invoke(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        context: CallContext,
        *,
        fault: FaultBehavior | None = None,
    ) -> Invocation:
        case = self.case
        case.seq += 1
        seq = case.seq
        number = case.call_counts.get(tool_name, 0) + 1
        case.call_counts[tool_name] = number
        args = copy.deepcopy(dict(arguments))
        tool = self.definition.tools.get(tool_name)
        record = CallRecord(
            seq=seq,
            call_number=number,
            tool=tool_name,
            arguments=args,
            http_status=0,
            status="error",
            risk=tool.risk if tool else None,
            expects_mutation=tool.expects_mutation if tool else False,
        )

        if tool is None:
            unknown = _Outcome(404, error_code="UNKNOWN_TOOL", message=f"No tool {tool_name}.")
            return self._finish(record, unknown.reply(), unknown, None)
        gate = self._gate(tool, args, context)
        if gate is not None:
            return self._finish(record, gate.reply(), gate, None)
        env = self._env(tool, args, context, seq, number)
        kind = fault.type if fault else None
        record.fault = kind

        if kind == "timeout_before_mutation":
            return self._finish(
                record, self._fault_error(fault, 504, "TIMEOUT", "The upstream timed out.", env), None, fault
            )
        if kind in ("http_429", "rate_limit"):
            reply = self._fault_error(fault, 429, "RATE_LIMITED", "Too many requests.", env)
            reply.headers["Retry-After"] = _seconds(fault.retry_after_s if fault else None, 1.0)
            return self._finish(record, reply, None, fault)
        if kind == "http_500":
            return self._finish(
                record, self._fault_error(fault, 500, "UPSTREAM_ERROR", "Upstream error.", env), None, fault
            )
        if kind == "http_503":
            reply = self._fault_error(fault, 503, "UNAVAILABLE", "The service is unavailable.", env)
            if fault and fault.retry_after_s is not None:
                reply.headers["Retry-After"] = _seconds(fault.retry_after_s, 1.0)
            return self._finish(record, reply, None, fault)
        if kind == "transient_unavailable":
            reply = self._fault_error(
                fault,
                503,
                "TRANSIENT_UNAVAILABLE",
                "The service is temporarily unavailable; retry later.",
                env,
            )
            reply.headers["Retry-After"] = _seconds(fault.retry_after_s if fault else None, 1.0)
            return self._finish(record, reply, None, fault)
        if kind == "permanent_unavailable":
            return self._finish(
                record,
                self._fault_error(
                    fault, 503, "PERMANENTLY_UNAVAILABLE", "The service is unavailable; do not retry.", env
                ),
                None,
                fault,
            )
        if kind == "non_retryable_error":
            return self._finish(
                record,
                self._fault_error(
                    fault, 422, "NON_RETRYABLE_ERROR", "The request cannot be completed; do not retry.", env
                ),
                None,
                fault,
            )
        if kind == "auth_denied":
            return self._finish(
                record,
                self._fault_error(fault, 403, "AUTH_DENIED", "The credentials were rejected.", env),
                None,
                fault,
            )
        if kind == "dropped_connection":
            return self._finish(record, Reply(0, None, transport="drop"), None, fault)

        mode: Mode = "normal"
        if kind == "success_without_mutation":
            mode = "no_mutation"
        elif kind == "inconsistent_state":
            mode = "first_effect_only"
        elif kind == "stale_response":
            mode = "stale"
        out = await self._execute(tool, args, context, env, mode)
        reply = out.reply()

        if kind == "timeout_after_mutation":
            reply = self._fault_error(fault, 504, "TIMEOUT", "The upstream timed out.", env)
        elif kind == "inconsistent_state" and out.ok:
            reply = self._fault_error(
                fault,
                500,
                "INCONSISTENT_STATE",
                "The operation failed after partially applying its changes.",
                env,
            )
        elif kind == "malformed_json":
            reply.transport = "malformed"
        elif kind == "partial_response":
            reply.transport = "partial"
        elif kind == "semantic_bad_response" and out.ok and fault is not None:
            env.result = out.result
            reply = Reply(out.status, {"result": render(fault.body, env)}, dict(out.headers))
        elif kind == "duplicate_response":
            previous = self.case.last_replies.get(tool_name)
            if previous is not None:
                reply = Reply(int(previous["status"]), previous["body"], dict(previous.get("headers") or {}))

        records = [record]
        invocation = self._finish(record, reply, out, fault)
        if kind == "message_duplication":
            # The network delivers the same request a second time. The agent
            # sees only the first reply; the twin executes both deliveries.
            self.case.seq += 1
            env2 = self._env(tool, args, context, self.case.seq, number)
            out2 = await self._execute(tool, args, context, env2, "normal")
            second = CallRecord(
                seq=self.case.seq,
                call_number=number,
                tool=tool_name,
                arguments=copy.deepcopy(args),
                http_status=out2.status,
                status=call_status(out2.status, "normal", out2.error_code),
                error_code=out2.error_code,
                response=out2.reply().body,
                risk=tool.risk,
                fault="message_duplication",
                mutated=out2.mutated,
                expects_mutation=tool.expects_mutation,
                replayed=out2.replayed,
                effect_key=self._effect_key(tool, env2),
                cross_tenant=out2.cross_tenant,
                policy_violation=out2.policy_violation,
                redelivered=True,
                changes=out2.changes,
            )
            records.append(second)
        invocation.records = records
        return invocation

    # -- pieces ---------------------------------------------------------------

    def _env(self, tool: ToolDef, args: Mapping[str, Any], ctx: CallContext, seq: int, number: int) -> Env:
        return Env(
            args=args,
            state=self.case.state,
            tenant=ctx.tenant,
            idempotency_key=self._idempotency_key(tool, args, ctx),
            now=ctx.now,
            seq=seq,
            call_number=number,
        )

    @staticmethod
    def _idempotency_key(tool: ToolDef, args: Mapping[str, Any], ctx: CallContext) -> str | None:
        if tool.idempotency_argument:
            value = args.get(tool.idempotency_argument)
            if isinstance(value, str) and value:
                return value
        if tool.idempotency_header:
            value = ctx.headers.get(tool.idempotency_header)
            if value:
                return value
        return None

    def _gate(self, tool: ToolDef, args: dict[str, Any], ctx: CallContext) -> _Outcome | None:
        """Contract and policy checks that come before the dependency is
        reached (faults do not apply to them)."""
        name = tool.name
        if ctx.allowed_tools is not None and name not in ctx.allowed_tools:
            return _Outcome(
                403,
                error_code="TOOL_NOT_ALLOWED",
                message=f"{name} is not allowed in this scenario.",
                policy_violation="TOOL_NOT_ALLOWED",
            )
        if tool.risk == "ADMIN" and ctx.caller_role != "admin":
            return _Outcome(
                403,
                error_code="ADMIN_ONLY",
                message=f"{name} is restricted to administrators.",
                policy_violation="ADMIN_ONLY",
            )
        try:
            size = len(canonical_json(args))
        except (TypeError, ValueError):
            return _Outcome(
                400, error_code="INVALID_ARGUMENT", message="The arguments are not valid JSON values."
            )
        if size > MAX_ARGUMENT_BYTES:
            return _Outcome(413, error_code="ARGUMENTS_TOO_LARGE", message="The arguments are too large.")
        if tool.input_schema is not None:
            try:
                problems = validation_errors(tool.input_schema, args, limit=3)
            except RegexTimeout:
                problems = ["(root): the input schema pattern timed out"]
            if problems:
                return _Outcome(422, error_code="INVALID_ARGUMENT", message="; ".join(problems))
        return None

    def _fault_error(
        self, fault: FaultBehavior | None, status: int, code: str, message: str, env: Env
    ) -> Reply:
        if fault is not None and fault.has_body:
            return Reply(status, render(fault.body, env), delay_ms=self._delay(fault))
        msg = fault.message if fault and fault.message else message
        return Reply(status, {"error": {"code": code, "message": msg}}, delay_ms=self._delay(fault))

    @staticmethod
    def _delay(fault: FaultBehavior | None) -> int:
        if fault is None:
            return 0
        if fault.delay_ms is not None:
            return fault.delay_ms
        return 1000 if fault.type == "delay" else 0

    def _effect_key(self, tool: ToolDef, env: Env) -> str | None:
        if not tool.effect_key:
            return None
        try:
            value = render(tool.effect_key, env)
        except TemplateError:
            return None
        return value if isinstance(value, str) else json.dumps(value, sort_keys=True)

    def _finish(
        self, record: CallRecord, reply: Reply, out: _Outcome | None, fault: FaultBehavior | None
    ) -> Invocation:
        if fault is not None and reply.delay_ms == 0:
            reply.delay_ms = self._delay(fault)
        body = reply.body
        record.http_status = 0 if reply.transport == "drop" else reply.status
        error = body.get("error") if isinstance(body, Mapping) else None
        record.error_code = (
            str(error.get("code")) if isinstance(error, Mapping) and error.get("code") else None
        )
        record.status = call_status(reply.status, reply.transport, record.error_code)
        record.response = None if reply.transport == "drop" else body
        record.delay_ms = reply.delay_ms
        if out is not None:
            record.mutated = out.mutated
            record.replayed = out.replayed
            record.changes = out.changes
            record.cross_tenant = out.cross_tenant
            record.policy_violation = out.policy_violation
        tool = self.definition.tools.get(record.tool)
        if tool is not None:
            env = Env(
                args=record.arguments,
                state=self.case.state,
                seq=record.seq,
                call_number=record.call_number,
            )
            record.effect_key = self._effect_key(tool, env)
            if reply.transport == "normal":
                self.case.last_replies[record.tool] = {
                    "status": reply.status,
                    "body": copy.deepcopy(reply.body),
                    "headers": dict(reply.headers),
                }
        return Invocation(reply=reply, records=[record])

    # -- execution --------------------------------------------------------------

    async def _execute(
        self, tool: ToolDef, args: dict[str, Any], ctx: CallContext, env: Env, mode: Mode
    ) -> _Outcome:
        key = env.idempotency_key
        slot = f"{tool.name}\n{key}" if key else None
        if slot is not None and slot in self.case.idempotency:
            prior = self.case.idempotency[slot]
            if prior["args_hash"] != _args_hash(args):
                return _Outcome(
                    409,
                    error_code="IDEMPOTENCY_CONFLICT",
                    message="This idempotency key was already used with different arguments.",
                )
            return _Outcome(
                int(prior["status"]),
                result=copy.deepcopy(prior["result"]),
                replayed=True,
                headers={"Idempotent-Replayed": "true"},
            )
        try:
            out = await self._run_handler(tool, args, ctx, env, mode)
        except _Stop as stop:
            out = _Outcome(stop.status, error_code=stop.code, message=stop.message, **stop.record)
        except TemplateError as err:
            if err.argument is not None:
                code = "MISSING_ARGUMENT" if err.missing else "INVALID_ARGUMENT"
                out = _Outcome(422, error_code=code, message=str(err))
            else:
                out = _Outcome(500, error_code="TWIN_TEMPLATE_ERROR", message=str(err))
        except RegexTimeout as err:
            out = _Outcome(500, error_code="TWIN_TEMPLATE_ERROR", message=str(err))
        if out.ok and tool.output_schema is not None:
            try:
                problems = validation_errors(tool.output_schema, out.result, limit=3)
            except RegexTimeout:
                problems = ["(root): the output schema pattern timed out"]
            if problems:
                out = _Outcome(
                    500,
                    error_code="TWIN_CONTRACT_VIOLATION",
                    message="The twin's response does not match the tool's output schema: "
                    + "; ".join(problems),
                )
        if (
            out.committed_state is not None
            and mode != "no_mutation"
            and (out.ok or mode == "first_effect_only")
        ):
            out.changes = diff_state(self.case.state, out.committed_state)
            self.case.state = out.committed_state
            env.state = self.case.state
        else:
            out.mutated = False if mode == "no_mutation" or not out.ok else out.mutated
        out.committed_state = None
        if slot is not None and out.ok and not out.replayed:
            self.case.idempotency[slot] = {
                "args_hash": _args_hash(args),
                "status": out.status,
                "result": copy.deepcopy(out.result),
            }
        tk = self.definition.tenant_key
        if (
            out.ok
            and tk
            and ctx.tenant
            and out.cross_tenant is None
            and _foreign_tenant(out.result, tk, ctx.tenant)
        ):
            out.cross_tenant = "allowed"
        return out

    async def _run_handler(
        self, tool: ToolDef, args: dict[str, Any], ctx: CallContext, env: Env, mode: Mode
    ) -> _Outcome:
        h = tool.handler
        self._check_tenant(tool, env, ctx)
        if h.kind == "read":
            base = copy.deepcopy(self.case.initial) if mode == "stale" else self.case.state
            segs = render_path(str(h.path), env)
            value = get_path(base, segs)
            if value is MISSING:
                raise _Stop(h.not_found.status, h.not_found.code, h.not_found.message)
            env.value = value
            result = render(h.response, env) if h.has_response else copy.deepcopy(value)
            return _Outcome(200, result=result)
        if h.kind == "static":
            return _Outcome(200, result=render(h.response, env) if h.has_response else {})
        if h.kind == "echo":
            return _Outcome(200, result={"tool": tool.name, "arguments": copy.deepcopy(args)})
        if h.kind == "recorded":
            for f in h.fixtures:
                if subset_match(f.match, args):
                    if f.error is not None:
                        raise _Stop(f.error.status, f.error.code, f.error.message or f.error.code)
                    return _Outcome(f.status, result=render(f.response, env))
            raise _Stop(404, "NO_FIXTURE", "No recorded response matches these arguments.")
        if h.kind == "custom":
            return await self._run_adapter(tool, args, ctx, env, mode)
        return self._run_mutate(tool, args, ctx, env, mode)

    def _check_tenant(self, tool: ToolDef, env: Env, ctx: CallContext) -> None:
        tk = self.definition.tenant_key
        if not (tool.tenant_path and tk and ctx.tenant):
            return
        try:
            segs = render_path(tool.tenant_path, env)
        except TemplateError as err:
            if err.missing:
                return  # the handler reports a missing required argument itself
            raise
        entity = get_path(self.case.state, segs)
        if isinstance(entity, Mapping) and tk in entity and entity[tk] != ctx.tenant:
            raise _Stop(
                403,
                "ACCESS_DENIED",
                "This record belongs to another tenant.",
                cross_tenant="denied",
                policy_violation="CROSS_TENANT",
            )

    def _run_mutate(
        self, tool: ToolDef, args: dict[str, Any], ctx: CallContext, env: Env, mode: Mode
    ) -> _Outcome:
        h = tool.handler
        scratch = copy.deepcopy(self.case.state)
        env.state = scratch
        segs: tuple[Segment, ...] | None = render_path(h.path, env) if h.path else None
        before = get_path(scratch, segs) if segs is not None else MISSING
        if segs is not None and before is MISSING:
            raise _Stop(h.not_found.status, h.not_found.code, h.not_found.message)
        before_copy = copy.deepcopy(before)
        env.value = before
        doc = {"args": args, "state": scratch, "value": before, "tenant": ctx.tenant}
        for cond in h.preconditions:
            actual = get_path(doc, render_path(cond.path, env))
            cmp = compare(actual, render(dict(cond.comparators), env))
            if not cmp.ok:
                err = cond.error or ErrorSpec(409, "PRECONDITION_FAILED", "")
                raise _Stop(err.status, err.code, err.message or f"{cond.path}: {cmp.reason}")
        effects = h.effects[:1] if mode == "first_effect_only" else h.effects
        try:
            for effect in effects:
                self._apply(scratch, effect, env)
        except (PathError, TypeError, ValueError) as err:
            if isinstance(err, TemplateError):
                raise
            raise _Stop(500, "TWIN_EFFECT_ERROR", f"The twin could not apply an effect: {err}") from None
        after = get_path(scratch, segs) if segs is not None else MISSING
        env.value = before_copy if mode == "stale" else after
        if h.has_response:
            result = render(h.response, env)
        elif segs is not None:
            result = copy.deepcopy(env.value)
        else:
            result = {"ok": True}
        return _Outcome(200, result=result, mutated=True, committed_state=scratch)

    def _apply(self, state: dict[str, Any], effect: Effect, env: Env) -> None:
        segs = render_path(effect.path, env)
        if not segs:
            raise PathError("an effect cannot replace the whole state")
        if effect.op == "set":
            set_path(state, segs, render(effect.value, env))
        elif effect.op in ("increment", "decrement"):
            by = render(effect.by, env) if isinstance(effect.by, str) else effect.by
            if isinstance(by, bool) or not isinstance(by, int | float):
                raise TypeError(f"{effect.op} needs a number, got {by!r}")
            current = get_path(state, segs)
            if current is MISSING or current is None:
                current = 0
            if isinstance(current, bool) or not isinstance(current, int | float):
                raise TypeError(f"{format_path(segs)} is not a number")
            value = current + by if effect.op == "increment" else current - by
            if isinstance(value, float):
                value = round(value, 10)
            set_path(state, segs, value)
        elif effect.op == "append":
            current = get_path(state, segs)
            if current is MISSING or current is None:
                current = []
                set_path(state, segs, current)
            if not isinstance(current, list):
                raise TypeError(f"{format_path(segs)} is not a list")
            current.append(render(effect.value, env))
        elif effect.op == "delete":
            delete_path(state, segs)
        elif effect.op == "merge":
            patch = render(effect.value, env)
            if not isinstance(patch, Mapping):
                raise TypeError("merge needs an object")
            current = get_path(state, segs)
            base = current if isinstance(current, Mapping) else {}
            set_path(state, segs, {**base, **copy.deepcopy(dict(patch))})
        else:  # pragma: no cover - the schema enumerates the operations
            raise ValueError(f"unknown effect {effect.op}")

    async def _run_adapter(
        self, tool: ToolDef, args: dict[str, Any], ctx: CallContext, env: Env, mode: Mode
    ) -> _Outcome:
        adapter = self.adapters.get(str(tool.handler.adapter))
        if adapter is None:
            raise _Stop(500, "TWIN_ADAPTER_MISSING", f"Adapter {tool.handler.adapter} is not registered.")
        scratch = copy.deepcopy(self.case.initial if mode == "stale" else self.case.state)
        res = await adapter.invoke(
            AdapterRequest(
                tool=tool,
                arguments=copy.deepcopy(args),
                state=scratch,
                tenant=ctx.tenant,
                now=ctx.now,
                seq=env.seq,
                call_number=env.call_number,
            )
        )
        if not 200 <= res.status < 300:
            return _Outcome(
                res.status,
                error_code=res.error_code or "ERROR",
                message=res.message,
                headers=dict(res.headers),
            )
        mutated = res.mutated and mode != "stale"
        return _Outcome(
            res.status,
            result=res.result,
            headers=dict(res.headers),
            mutated=mutated,
            committed_state=scratch if mutated else None,
        )


def _seconds(value: float | None, default: float) -> str:
    v = default if value is None else value
    return str(int(v)) if float(v).is_integer() else f"{v:g}"
