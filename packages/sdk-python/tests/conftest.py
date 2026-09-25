from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agenttwin import AgentTwin, Config

sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def make_client(exporter: InMemorySpanExporter) -> Iterator[Any]:
    clients: list[AgentTwin] = []

    def make(**overrides: Any) -> AgentTwin:
        values: dict[str, Any] = {
            "service_name": "test-agent",
            "environment": "test",
            "schedule_delay_ms": 50,
        }
        values.update(overrides)
        cfg = Config(**values)
        client = AgentTwin(cfg, exporter=exporter)
        clients.append(client)
        return client

    yield make
    for c in clients:
        c.shutdown()


@pytest.fixture(scope="session")
def go_parity() -> Iterator[Any]:
    """Runs requests through the Go reference implementation
    (packages/gokit/cmd/parity). Skipped when Go is unavailable unless
    AGENTTWIN_REQUIRE_PARITY=1."""
    go = shutil.which("go")
    if go is None:
        if os.environ.get("AGENTTWIN_REQUIRE_PARITY") == "1":
            pytest.fail("Go toolchain required for parity tests")
        pytest.skip("Go toolchain not available")
    out = REPO / ".artifacts" / "parity"
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([go, "build", "-o", str(out), "./packages/gokit/cmd/parity"], cwd=REPO, check=True)  # noqa: S603

    def run(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        proc = subprocess.run(  # noqa: S603
            [str(out)], input=json.dumps(requests).encode("utf-8"), capture_output=True, check=True
        )
        result: list[dict[str, Any]] = json.loads(proc.stdout)
        return result

    yield run
