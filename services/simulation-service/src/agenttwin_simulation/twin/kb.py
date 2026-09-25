"""The twin's knowledge base (retrieval) for ``GET /kb/search``.

Documents come from the twin state (``spec.retrieval.statePath``, default
``kb``: a list of ``{id, title, text}``) and from the scenario's
``spec.input.documents`` (``{id, content, trusted}``), which is how a scenario
plants content - including malicious instructions - for the agent to find.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from agenttwin_core.paths import MISSING, PathError, get_path

__all__ = ["MAX_RESULTS", "collect_documents", "search"]

MAX_RESULTS = 10
_TERM = re.compile(r"\w+", re.UNICODE)


def collect_documents(
    state: Mapping[str, Any], state_path: str, scenario_documents: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    try:
        stored = get_path(state, state_path)
    except PathError:
        stored = MISSING
    if isinstance(stored, list):
        for d in stored:
            if isinstance(d, Mapping) and isinstance(d.get("id"), str):
                docs.append(
                    {
                        "id": d["id"],
                        "title": str(d.get("title") or d["id"]),
                        "text": str(d.get("text") or d.get("content") or ""),
                    }
                )
    for d in scenario_documents:
        docs.append(
            {
                "id": str(d["id"]),
                "title": str(d.get("title") or d["id"]),
                "text": str(d.get("content") or ""),
                "trusted": bool(d.get("trusted", False)),
            }
        )
    return docs


def search(documents: Sequence[Mapping[str, Any]], query: str, limit: int = 3) -> list[dict[str, Any]]:
    """Term-frequency ranking (ties by id): deterministic and good enough to
    decide *which* documents an agent sees."""
    terms = {t for t in _TERM.findall(query.casefold()) if len(t) > 2}
    scored: list[tuple[int, str, Mapping[str, Any]]] = []
    for doc in documents:
        text = f"{doc.get('title', '')} {doc.get('text', '')}".casefold()
        words = _TERM.findall(text)
        score = sum(1 for w in words if w in terms)
        if score:
            scored.append((score, str(doc.get("id")), doc))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [dict(d) for _, _, d in scored[: max(1, min(limit, MAX_RESULTS))]]
