"""Custom twin adapters (spec §24 "custom adapter", §85).

A developer implements domain-specific behavior in Python and registers it
by name in code; a twin tool selects it with ``handler: {kind: custom,
adapter: <name>}``. There is no dynamic loading from configuration.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from agenttwin_simulation.twin.definition import ToolDef

__all__ = ["Adapter", "AdapterRegistry", "AdapterRequest", "AdapterResult"]


@dataclass(frozen=True)
class AdapterRequest:
    tool: ToolDef
    arguments: Mapping[str, Any]
    # A private copy of the twin state; changes are committed only when the
    # result says ``mutated`` (and the call is not a success-without-mutation fault).
    state: dict[str, Any]
    tenant: str | None
    now: str
    seq: int
    call_number: int


@dataclass
class AdapterResult:
    status: int = 200
    result: Any = None
    error_code: str | None = None
    message: str = ""
    mutated: bool = False
    headers: dict[str, str] = field(default_factory=dict)


class Adapter(Protocol):
    name: str
    version: str

    async def invoke(self, request: AdapterRequest) -> AdapterResult: ...


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, Adapter] = {}

    def register(self, adapter: Adapter) -> None:
        if adapter.name in self._adapters:
            raise ValueError(f"adapter {adapter.name!r} is already registered")
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> Adapter | None:
        return self._adapters.get(name)

    def names(self) -> list[str]:
        return sorted(self._adapters)
