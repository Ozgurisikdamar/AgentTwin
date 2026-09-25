from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
# The integration harness (eval_testutil) is imported by the test modules; it
# builds on the simulation service's harness (sim_testutil): an evaluation
# drives real simulation runs.
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parents[1] / "simulation-service" / "tests"))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
