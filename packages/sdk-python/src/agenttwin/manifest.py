"""Agent manifest helper.

Loads an ``agenttwin.dev/v1`` agent manifest (YAML needs the optional PyYAML
dependency; JSON always works) and exposes what instrumentation needs: name,
version, prompt hash and declared tool risks. Validation and the canonical
manifest identity are computed server-side when the version is registered.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agenttwin.hashing import sha256_hex

__all__ = ["AgentManifest", "load_manifest"]

API_VERSION = "agenttwin.dev/v1"
_MAX_BYTES = 1 << 20


@dataclass(frozen=True)
class AgentManifest:
    name: str
    version: str
    instructions: str
    model_provider: str | None
    model_name: str | None
    tool_risks: Mapping[str, str] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def prompt_hash(self) -> str:
        """SHA-256 of the instruction text (same as the control plane)."""
        return sha256_hex(self.instructions)

    def risk_of(self, tool: str) -> str | None:
        return self.tool_risks.get(tool)


def load_manifest(path: str | Path) -> AgentManifest:
    """Load a manifest file (``.json``, ``.yaml`` or ``.yml``)."""
    p = Path(path)
    data = p.read_bytes()
    if len(data) > _MAX_BYTES:
        raise ValueError("manifest larger than 1 MiB")
    if p.suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as err:  # pragma: no cover - dependency is optional
            raise RuntimeError("reading YAML manifests requires PyYAML") from err
        doc = yaml.safe_load(data)
    else:
        doc = json.loads(data)
    return parse_manifest(doc)


def parse_manifest(doc: Any) -> AgentManifest:
    if not isinstance(doc, Mapping):
        raise ValueError("manifest must be an object")
    if doc.get("apiVersion") != API_VERSION or doc.get("kind") != "Agent":
        raise ValueError(f"manifest must have apiVersion {API_VERSION} and kind Agent")
    meta = doc.get("metadata") or {}
    spec = doc.get("spec") or {}
    name, version = meta.get("name"), meta.get("version")
    if not isinstance(name, str) or not isinstance(version, str) or not name or not version:
        raise ValueError("metadata.name and metadata.version are required")
    risks: dict[str, str] = {}
    for tool in spec.get("tools") or []:
        if not isinstance(tool, Mapping) or not isinstance(tool.get("name"), str):
            continue
        risk = tool.get("risk")
        level = risk.get("level") if isinstance(risk, Mapping) else risk
        if isinstance(level, str):
            risks[tool["name"]] = level.upper()
    model = spec.get("model") or {}
    return AgentManifest(
        name=name,
        version=version,
        instructions=str(spec.get("instructions") or ""),
        model_provider=model.get("provider"),
        model_name=model.get("name"),
        tool_risks=risks,
        raw=doc,
    )
