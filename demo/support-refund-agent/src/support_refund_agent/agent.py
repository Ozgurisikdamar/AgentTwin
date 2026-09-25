"""The support/refund agent: a small, framework-neutral tool-calling loop.

Each run is one AgentTwin trace: ``invoke_agent`` root, a ``chat`` span per
model turn, a tool span per tool call (recorded by the tool client), a
retrieval span for knowledge-base lookups, and the agent's own outcome
report - which is a *claim*, never verification.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agenttwin import AgentManifest, AgentTwin, load_manifest
from agenttwin.gateway import Gateway
from support_refund_agent.models import KB_TOOL, ChatModel, Message, ScriptedPlannerModel, tool_specs
from support_refund_agent.tool_client import ToolClient, ToolOutcome

__all__ = [
    "Agent",
    "ContainmentUnavailable",
    "ManifestStore",
    "RunRequest",
    "RunResult",
    "default_manifest_dir",
]


class ContainmentUnavailable(ValueError):
    """A contained run was asked of an agent without a runtime gateway."""


def default_manifest_dir() -> Path:
    env = os.environ.get("DEMO_AGENT_MANIFEST_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "manifests"


class ManifestStore:
    """The agent versions this deployment can run (one manifest each)."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or default_manifest_dir()
        self._by_version: dict[str, AgentManifest] = {}
        self._paths: dict[str, Path] = {}
        for path in sorted(self.directory.glob("*.yaml")):
            m = load_manifest(path)
            self._by_version[m.version] = m
            self._paths[m.version] = path
        if not self._by_version:
            raise FileNotFoundError(f"no agent manifests in {self.directory}")

    @property
    def versions(self) -> list[str]:
        return sorted(self._by_version, key=lambda v: tuple(int(p) for p in v.split(".")))

    def path(self, version: str) -> Path:
        """The manifest file of ``version`` (registered with the platform)."""
        return self._paths[version]

    def get(self, version: str | None) -> AgentManifest:
        if version is None:
            return self._by_version[self.versions[-1]]
        try:
            return self._by_version[version]
        except KeyError:
            raise KeyError(f"unknown agent version {version!r}; known: {self.versions}") from None


def _attempt(calls: list[dict[str, Any]], name: str, arguments: dict[str, Any]) -> int:
    """1 for a new call, n+1 after n failures of the same call (same tool and
    arguments) since it last succeeded. Repeating a call that succeeded - a
    deliberate re-read, e.g. to confirm that a refund was recorded - is a new
    call, not a retry."""
    failures = 0
    for c in reversed(calls):
        if c["name"] != name or c["arguments"] != arguments:
            continue
        if c["status"] == "ok":
            break
        failures += 1
    return failures + 1


@dataclass
class RunRequest:
    input: str
    customer_id: str | None = None
    tenant: str = "demo-co"
    session_id: str | None = None
    version: str | None = None
    tools_base_url: str | None = None
    tool_headers: dict[str, str] = field(default_factory=dict)
    source: str | None = None
    environment: str | None = None
    release_id: str | None = None
    simulation_run_id: str | None = None
    scenario_id: str | None = None
    #: Call the tools through the runtime gateway (ADR-0033).
    contained: bool = False

    @classmethod
    def from_json(cls, body: dict[str, Any]) -> RunRequest:
        ctx = body.get("run_context") or {}
        text = body.get("input")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("input must be a non-empty string")
        if len(text) > 8000:
            raise ValueError("input is too long")
        headers = body.get("tool_headers") or {}
        if not isinstance(headers, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
        ):
            raise ValueError("tool_headers must map strings to strings")
        contained = body.get("contained", False)
        if not isinstance(contained, bool):
            raise ValueError("contained must be a boolean")
        return cls(
            input=text,
            customer_id=body.get("customer_id"),
            tenant=str(body.get("tenant") or "demo-co"),
            session_id=body.get("session_id"),
            version=body.get("agent_version") or body.get("version"),
            tools_base_url=body.get("tools_base_url"),
            tool_headers=headers,
            source=ctx.get("source"),
            environment=ctx.get("environment"),
            release_id=ctx.get("release_id"),
            simulation_run_id=ctx.get("simulation_run_id"),
            scenario_id=ctx.get("scenario_id"),
            contained=contained,
        )


@dataclass
class RunResult:
    output: str
    trace_id: str
    status: str  # completed | step_limit | tool_limit | time_limit
    agent_version: str
    model: str
    model_kind: str
    steps: int
    claimed_outcome: str
    business_outcome: str | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


ModelFactory = Callable[[AgentManifest, RunRequest], ChatModel]


def scripted_model_factory(secret: str) -> ModelFactory:
    def make(manifest: AgentManifest, req: RunRequest) -> ChatModel:
        return ScriptedPlannerModel(
            manifest.model_name or "scripted-planner-v1",
            customer_id=req.customer_id,
            secret=secret,
            contained=req.contained,
        )

    return make


class Agent:
    def __init__(
        self,
        telemetry: AgentTwin,
        manifests: ManifestStore,
        *,
        tools_base_url: str,
        model_factory: ModelFactory,
        tool_timeout_s: float = 1.5,
        backoff_scale: float = 1.0,
        gateway_url: str | None = None,
        gateway_api_key: str | None = None,
        approval_wait_s: float = 0.0,
    ) -> None:
        self.telemetry = telemetry
        self.manifests = manifests
        self.tools_base_url = tools_base_url
        self.model_factory = model_factory
        self.tool_timeout_s = tool_timeout_s
        self.backoff_scale = backoff_scale
        # Where contained runs send their tool calls (the control plane) and
        # the project key they authenticate with.
        self.gateway_url = gateway_url
        self.gateway_api_key = gateway_api_key
        self.approval_wait_s = approval_wait_s

    def _gateway(self, manifest: AgentManifest, req: RunRequest) -> Gateway | None:
        if not req.contained:
            return None
        if not self.gateway_url or not self.gateway_api_key:
            raise ContainmentUnavailable(
                "this agent has no runtime gateway configured (DEMO_AGENT_GATEWAY_URL and AGENTTWIN_API_KEY)"
            )
        return Gateway(
            self.gateway_url,
            self.gateway_api_key,
            agent=manifest.name,
            agent_version=manifest.version,
            environment=req.environment or "production",
        )

    def run(self, req: RunRequest) -> RunResult:
        manifest = self.manifests.get(req.version)
        spec = manifest.raw.get("spec", {})
        limits = spec.get("limits") or {}
        max_steps = int(limits.get("maxSteps", 20))
        max_tool_calls = int(limits.get("maxToolCalls", 15))
        max_duration = float(limits.get("maxDurationSeconds", 120))
        model = self.model_factory(manifest, req)
        tools = ToolClient(
            base_url=req.tools_base_url or self.tools_base_url,
            tenant=req.tenant,
            risks=dict(manifest.tool_risks),
            headers=req.tool_headers,
            timeout_s=self.tool_timeout_s,
            gateway=self._gateway(manifest, req),
            approval_wait_s=self.approval_wait_s,
        )
        specs = tool_specs(
            spec.get("tools") or [], retrieval=bool((spec.get("retrieval") or {}).get("sources"))
        )
        temperature = (spec.get("model") or {}).get("temperature")
        session = req.session_id or f"sess-{uuid.uuid4().hex[:12]}"
        calls: list[dict[str, Any]] = []
        started = time.monotonic()

        with self.telemetry.agent_run(
            manifest.name,
            manifest.version,
            input=req.input,
            # The request context a production failure's scenario replays (ADR-0032).
            input_context={k: v for k, v in (("tenant", req.tenant), ("customer_id", req.customer_id)) if v},
            session_id=session,
            source=req.source,  # type: ignore[arg-type]
            environment=req.environment,
            release_id=req.release_id,
            simulation_run_id=req.simulation_run_id,
            scenario_id=req.scenario_id,
            attributes={"agenttwin.model.kind": model.kind, "agenttwin.runtime.contained": req.contained},
        ) as run:
            messages: list[Message] = [{"role": "user", "content": req.input}]
            status, final = "step_limit", "I wasn't able to finish this request; a specialist will follow up."
            claimed, business = "UNKNOWN", None
            steps = 0
            for steps in range(1, max_steps + 1):  # noqa: B007 - steps is reported
                if time.monotonic() - started > max_duration:
                    status = "time_limit"
                    break
                with run.model_call(
                    model.provider,
                    model.name,
                    input_messages=[_display(m) for m in messages],
                    system_instructions=manifest.instructions,
                    temperature=temperature,
                    prompt_hash=manifest.prompt_hash,
                    prompt_version=manifest.version,
                ) as mc:
                    resp = model.chat(manifest.instructions, messages, specs)
                    out: dict[str, Any] = {"role": "assistant", "content": resp.text or ""}
                    if resp.tool_calls:
                        out["tool_calls"] = [
                            {"name": t.name, "arguments": t.arguments} for t in resp.tool_calls
                        ]
                    mc.record_response(
                        output_messages=[out],
                        input_tokens=resp.input_tokens,
                        output_tokens=resp.output_tokens,
                        finish_reasons=[resp.stop_reason],
                        response_model=resp.response_model or model.name,
                    )
                if not resp.tool_calls:
                    status, final = "completed", resp.text or ""
                    claimed, business = resp.claimed_outcome or "UNKNOWN", resp.business_outcome
                    break
                messages.append({"role": "assistant", "content": resp.text, "tool_calls": resp.tool_calls})
                for tc in resp.tool_calls:
                    if len(calls) >= max_tool_calls:
                        status = "tool_limit"
                        break
                    self._backoff(calls, tc.name)
                    attempt = _attempt(calls, tc.name, tc.arguments)
                    if tc.name == KB_TOOL:
                        docs = tools.search_kb(run, str(tc.arguments.get("query", "")))
                        outcome = ToolOutcome(status="ok", result=docs)
                    else:
                        outcome = tools.call(run, tc.name, tc.arguments, attempt=attempt, call_id=tc.id)
                    calls.append(
                        {
                            "name": tc.name,
                            "arguments": tc.arguments,
                            "status": outcome.status,
                            "error_code": outcome.error_code,
                            "retry_after": outcome.retry_after,
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": tc.name,
                            "content": outcome.to_message(),
                        }
                    )
                if status == "tool_limit":
                    break
            if status != "completed":
                run.set_error(status, f"agent stopped: {status}")
                claimed, business = "FAILURE", "LIMIT_EXCEEDED"
            run.set_output(final)
            # The agent's own report is a claim, never verification (spec §15).
            run.outcome(claimed, claimed=claimed, business_outcome=business)  # type: ignore[arg-type]
            return RunResult(
                output=final,
                trace_id=run.trace_id,
                status=status,
                agent_version=manifest.version,
                model=model.name,
                model_kind=model.kind,
                steps=steps,
                claimed_outcome=claimed,
                business_outcome=business,
                tool_calls=calls,
            )

    def _backoff(self, calls: list[dict[str, Any]], tool: str) -> None:
        """Bounded exponential backoff after rate limiting."""
        streak = 0
        for c in reversed(calls):
            if c["name"] != tool or c["status"] != "rate_limited":
                break
            streak += 1
        if streak:
            last = next(c for c in reversed(calls) if c["name"] == tool)
            base = float(last.get("retry_after") or 1.0)
            time.sleep(min(base * (2 ** (streak - 1)), 8.0) * self.backoff_scale)


def _display(m: Message) -> dict[str, Any]:
    """A JSON-safe view of a message for (policy-permitting) capture."""
    if m["role"] == "assistant" and m.get("tool_calls"):
        return {
            "role": "assistant",
            "content": m.get("content") or "",
            "tool_calls": [{"name": t.name, "arguments": t.arguments} for t in m["tool_calls"]],
        }
    return {k: v for k, v in m.items() if k in ("role", "content", "name", "tool_call_id")}
