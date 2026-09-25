"""Each Python service image holds the schema documents its code reads.

The service tests run from the repository, where every document is found
next to the code; an image holds only what its Dockerfile stage copies. A
module that validates against a schema the stage leaves out imports fine in
the tests and fails the container at start (this happened: the evaluation
service validates promoted regressions as scenarios). Here each service is
imported, in a fresh interpreter, with ``AGENTTWIN_SCHEMA_DIR`` pointing to
exactly what its stage copies to ``/app/schemas``."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "infra" / "docker" / "python.Dockerfile"
SCHEMAS = "/app/schemas/"
# stage -> modules the image runs (serve and worker share one entry point).
SERVICES = {
    "simulation-service": ["agenttwin_simulation.main"],
    "evaluation-service": ["agenttwin_evaluation.main"],
}


def stage_copies(stage: str) -> list[tuple[str, str]]:
    """The ``COPY <repo path> /app/schemas/<dest>`` lines of a stage."""
    text = DOCKERFILE.read_text()
    m = re.search(rf"^FROM \S+ AS {re.escape(stage)}\n(.*?)(?=^FROM |\Z)", text, re.M | re.S)
    assert m, f"no stage {stage} in {DOCKERFILE}"
    out = []
    for line in m.group(1).splitlines():
        parts = line.split()
        if parts[:1] == ["COPY"] and len(parts) == 3 and parts[2].startswith(SCHEMAS):
            out.append((parts[1], parts[2].removeprefix(SCHEMAS)))
    return out


def test_the_stages_copy_the_contracts() -> None:
    for stage in SERVICES:
        assert ("packages/contracts", "contracts") in stage_copies(stage), stage


@pytest.mark.parametrize("stage", sorted(SERVICES))
def test_a_service_starts_with_only_what_its_image_holds(stage: str, tmp_path: Path) -> None:
    for source, dest in stage_copies(stage):
        shutil.copytree(REPO / source, tmp_path / dest)
    env = {k: v for k, v in os.environ.items() if k != "AGENTTWIN_SCHEMA_DIR"}
    env["AGENTTWIN_SCHEMA_DIR"] = str(tmp_path)
    for module in SERVICES[stage]:
        # Imported from outside the repository, so that no document is found
        # next to the code.
        run = subprocess.run(  # noqa: S603 - a fixed interpreter and module name
            [sys.executable, "-c", f"import {module}"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert run.returncode == 0, f"{stage}: import {module} failed\n{run.stderr[-2000:]}"
