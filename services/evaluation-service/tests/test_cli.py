"""The ``evaluation-service`` command answers ``healthcheck`` without importing the
service (the container runs it every few seconds) and runs the service for
everything else."""

from __future__ import annotations

import os
import socket
import subprocess
import sys

from agenttwin_evaluation import cli
from agenttwin_evaluation.main import SPEC

HEAVY = ("fastapi", "psycopg", "uvicorn", "agenttwin_evaluation.main")
# Runs the command in a fresh interpreter and reports what it imported.
PROGRAM = f"""
import sys
from agenttwin_evaluation.cli import main
sys.argv = ["evaluation-service", *sys.argv[1:]]
try:
    main()
finally:
    print("imported=" + ",".join(m for m in {HEAVY!r} if m in sys.modules), file=sys.stderr)
"""


def run(*args: str, port: int | None = None) -> tuple[int, str]:
    env = {**os.environ}
    env.pop("PORT", None)
    if port is not None:
        env["PORT"] = str(port)
    done = subprocess.run(  # noqa: S603 - this interpreter, a fixed program
        [sys.executable, "-c", PROGRAM, *args], capture_output=True, text=True, env=env, timeout=60
    )
    return done.returncode, done.stderr


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
    return port


def test_the_command_probes_the_services_own_port() -> None:
    assert SPEC.default_port == cli.DEFAULT_PORT


def test_healthcheck_is_answered_without_importing_the_service() -> None:
    code, err = run("healthcheck", "live", port=free_port())
    assert code == 1  # nothing listens there
    assert err.startswith("healthcheck: ")
    assert "imported=\n" in err


def test_every_other_command_runs_the_service() -> None:
    code, err = run("no-such-role")
    assert code == 2
    assert "usage: evaluation-service" in err
    assert "fastapi" in err.rsplit("imported=", 1)[1]
