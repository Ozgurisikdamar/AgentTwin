from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The factories and the integration harness (eval_testutil) are imported by
# the test modules.
sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
