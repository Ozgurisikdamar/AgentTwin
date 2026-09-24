from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def go_parity() -> Iterator[Callable[[list[dict[str, Any]]], list[dict[str, Any]]]]:
    """Runs requests through the Go reference implementation
    (packages/gokit/cmd/parity). Skipped without Go unless
    AGENTTWIN_REQUIRE_PARITY=1."""
    go = shutil.which("go")
    if go is None:
        if os.environ.get("AGENTTWIN_REQUIRE_PARITY") == "1":
            pytest.fail("Go toolchain required for parity tests")
        pytest.skip("Go toolchain not available")
    out = REPO / ".artifacts" / "parity"
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([go, "build", "-o", str(out), "./packages/gokit/cmd/parity"], cwd=REPO, check=True)  # noqa: S603

    def run(reqs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        res = subprocess.run(  # noqa: S603
            [str(out)], input=json.dumps(reqs), capture_output=True, text=True, check=True
        )
        result: list[dict[str, Any]] = json.loads(res.stdout)
        return result

    yield run
