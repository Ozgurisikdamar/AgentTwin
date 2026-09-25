"""Runs the security tests of spec §63, item by item.

The manifest (scripts/security-tests.yaml) names, for every item of §63, the
automated tests that cover it, across Go, Python and the web. This script
checks that every named test exists (a renamed or deleted test must not
silently leave an item uncovered), then runs them:

    uv run python scripts/security_tests.py            # Go, Python, web unit
    uv run python scripts/security_tests.py --live     # + browser checks (running stack)
    uv run python scripts/security_tests.py --list     # the items and their tests

Go and Python integration tests need AGENTTWIN_TEST_DATABASE_URL and
AGENTTWIN_TEST_AMQP_URL (``make test-security`` runs this inside
scripts/with-test-infra.sh); without them they skip, so the run fails when
it would otherwise skip one.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "scripts" / "security-tests.yaml"
WEB = ROOT / "apps" / "web"


@dataclass
class Item:
    id: str
    title: str
    go: dict[str, list[str]] = field(default_factory=dict)
    python: dict[str, list[str]] = field(default_factory=dict)
    web: list[str] = field(default_factory=list)
    e2e: list[str] = field(default_factory=list)
    note: str = ""


def load(path: Path = MANIFEST) -> list[Item]:
    doc: dict[str, Any] = yaml.safe_load(path.read_text())
    return [Item(**raw) for raw in doc["items"]]


def missing(items: list[Item], root: Path = ROOT) -> list[str]:
    """Every test the manifest names that does not exist, as readable lines."""
    problems: list[str] = []
    for item in items:
        if not (item.go or item.python or item.web or item.e2e):
            problems.append(f"{item.id}: no tests")
        for pkg, names in item.go.items():
            files = sorted((root / pkg).glob("*_test.go"))
            text = "\n".join(f.read_text() for f in files)
            if not files:
                problems.append(f"{item.id}: no Go test files in {pkg}")
            for name in names:
                if not re.search(rf"^func {re.escape(name)}\(t \*testing\.T\)", text, re.M):
                    problems.append(f"{item.id}: {pkg}: no func {name}")
        for file, names in item.python.items():
            p = root / file
            text = p.read_text() if p.is_file() else ""
            if not text:
                problems.append(f"{item.id}: no file {file}")
            for name in names:
                if not re.search(rf"^(async )?def {re.escape(name)}\(", text, re.M):
                    problems.append(f"{item.id}: {file}: no def {name}")
        for file in item.web + item.e2e:
            if not (root / "apps" / "web" / file).is_file():
                problems.append(f"{item.id}: no web test file {file}")
    return problems


def run(cmd: list[str], cwd: Path = ROOT) -> int:
    print("$", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=cwd)  # noqa: S603 - fixed commands; test names come from the manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--live", action="store_true", help="also run the browser checks (needs a running stack)"
    )
    parser.add_argument("--list", action="store_true", help="print the items and their tests")
    args = parser.parse_args(argv)

    items = load()
    if args.list:
        for item in items:
            print(f"{item.id}: {item.title}")
            for pkg, names in item.go.items():
                print(f"  go      {pkg}: {', '.join(names)}")
            for file, names in item.python.items():
                print(f"  python  {file}: {', '.join(names)}")
            for file in item.web:
                print(f"  web     {file}")
            for file in item.e2e:
                print(f"  e2e     {file}")
            if item.note:
                print(f"  note    {item.note}")
        return 0

    problems = missing(items)
    if problems:
        print("The manifest names tests that do not exist:", *problems, sep="\n  ", file=sys.stderr)
        return 2
    for var in ("AGENTTWIN_TEST_DATABASE_URL", "AGENTTWIN_TEST_AMQP_URL"):
        if not os.environ.get(var):
            print(f"{var} is not set: integration tests would skip (use make test-security)", file=sys.stderr)
            return 2

    go: dict[str, set[str]] = defaultdict(set)
    python: list[str] = []
    web: set[str] = set()
    e2e: set[str] = set()
    for item in items:
        for pkg, names in item.go.items():
            go[pkg].update(names)
        for file, names in item.python.items():
            python.extend(f"{file}::{name}" for name in names)
        web.update(item.web)
        e2e.update(item.e2e)

    failed: list[str] = []
    for pkg in sorted(go):
        pattern = "^(" + "|".join(sorted(go[pkg])) + ")$"
        if run(["go", "test", "-count=1", "-run", pattern, "./" + pkg]) != 0:
            failed.append(f"go {pkg}")
    if (
        python
        and run(["uv", "run", "pytest", "-q", "-rs", "-p", "no:cacheprovider", "--no-header", *python]) != 0
    ):
        failed.append("python")
    if web and run(["pnpm", "exec", "vitest", "run", *sorted(web)], cwd=WEB) != 0:
        failed.append("web")
    if args.live and e2e and run(["pnpm", "exec", "playwright", "test", *sorted(e2e)], cwd=WEB) != 0:
        failed.append("e2e")
    if failed:
        print("security tests failed:", ", ".join(failed), file=sys.stderr)
        return 1
    print(
        f"security tests passed: {len(items)} items of spec §63"
        + ("" if args.live else " (browser checks: --live)")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
