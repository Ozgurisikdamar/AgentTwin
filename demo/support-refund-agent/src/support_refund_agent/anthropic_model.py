"""Anthropic adapter implementing the same ChatModel interface as the
scripted planner (spec §50). Opt-in: ``DEMO_AGENT_MODEL=anthropic`` and
``ANTHROPIC_API_KEY``. Runs with a real model are nondeterministic and are
labeled ``llm`` in traces and run records."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from support_refund_agent.models import Message, ModelResponse, ToolCallRequest, ToolSpec

__all__ = ["AnthropicModel", "to_anthropic_messages"]


def to_anthropic_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Convert the neutral message list to Anthropic's content-block format."""
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments})
            out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            # Consecutive tool results belong to one user turn.
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out


class AnthropicModel:
    provider = "anthropic"
    kind = "llm"

    def __init__(
        self, model: str, *, client: Any = None, api_key: str | None = None, max_tokens: int = 1024
    ) -> None:
        self.name = model
        self.max_tokens = max_tokens
        if client is None:
            import anthropic  # optional dependency: support-refund-agent[anthropic]

            client = anthropic.Anthropic(api_key=api_key)
        self._client = client

    def chat(self, system: str, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelResponse:
        resp = self._client.messages.create(
            model=self.name,
            system=system,
            max_tokens=self.max_tokens,
            temperature=0,
            messages=to_anthropic_messages(messages),
            tools=[
                {"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools
            ],
        )
        text_parts: list[str] = []
        calls: list[ToolCallRequest] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else json.loads(block.input)
                calls.append(ToolCallRequest(id=block.id, name=block.name, arguments=args))
        stop = "tool_use" if calls else ("max_tokens" if resp.stop_reason == "max_tokens" else "end_turn")
        usage = getattr(resp, "usage", None)
        return ModelResponse(
            text="\n".join(text_parts) or None,
            tool_calls=calls,
            stop_reason=stop,  # type: ignore[arg-type]
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            response_model=getattr(resp, "model", self.name),
            # A real model does not self-classify; the claim is "completed".
            claimed_outcome=None if calls else "UNKNOWN",
        )
