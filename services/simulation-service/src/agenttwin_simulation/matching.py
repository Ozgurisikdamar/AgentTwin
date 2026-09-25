"""What a scenario is about, as text to embed, and the selection of the
scenarios a change touches (spec §22 "Semantic scenario matching").

The service answers facts about scenarios — which ones carry a name, a tag
or a source, and which ones are semantically close to a text — and says why
each one was chosen. It does not decide what a change requires: the control
plane combines these facts with the dependency graph and the gate policy.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, get_args

from agenttwin_core.embeddings import MAX_TEXT_CHARS

__all__ = ["MAX_MATCHED", "RECIPE", "SCENARIO_SOURCES", "ScenarioSource", "scenario_text"]

# How scenario_text builds the text. A stored vector of another recipe is
# computed again, so changing what the text contains needs a new recipe.
RECIPE = "scenario-text-v1"

ScenarioSource = Literal[
    "manual", "production_regression", "generated", "policy_derived", "imported", "production_replay"
]
SCENARIO_SOURCES: tuple[str, ...] = get_args(ScenarioSource)

# Scenarios one match answers at most (severest first).
MAX_MATCHED = 2000


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence):
        return [v for v in value if isinstance(v, str)]
    return []


def scenario_text(doc: Mapping[str, Any]) -> str:
    """The words a scenario is about: its name, description and tags, what
    it covers ("tool:refund_payment" → "refund_payment"), the customer's
    message, the faults it injects and what it expects. Documents are
    validated before they are stored; this reads them defensively anyway."""
    meta = doc.get("metadata") or {}
    spec = doc.get("spec") or {}
    parts: list[str] = [str(meta.get("name") or "").replace("-", " ")]
    parts += _strings(meta.get("description"))
    parts.append(" ".join(_strings(meta.get("tags"))))
    parts.append(" ".join(c.split(":", 1)[-1] for c in _strings(spec.get("covers"))))
    parts += _strings((spec.get("input") or {}).get("message"))
    for fault in spec.get("faults") or []:
        if isinstance(fault, Mapping):
            behavior = fault.get("behavior") or {}
            parts.append(" ".join([*_strings(fault.get("target")), *_strings(behavior.get("type"))]))
    for exp in spec.get("expectations") or []:
        if isinstance(exp, Mapping):
            fields = ("id", "type", "description", "tool", "tools")
            parts.append(" ".join(s for f in fields for s in _strings(exp.get(f))))
    text = "\n".join(p.strip() for p in parts if p and p.strip())
    return text[:MAX_TEXT_CHARS]
