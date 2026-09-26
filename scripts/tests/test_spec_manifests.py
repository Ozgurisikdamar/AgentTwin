"""The spec manifests (scripts/security-tests.yaml for §63,
scripts/chaos-tests.yaml for §64) cover every item of their section with
tests that exist: a renamed or deleted test must not leave an item silently
uncovered (``make test-security`` and ``make test-chaos`` run them)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("spec_tests", ROOT / "scripts" / "spec_tests.py")
assert _spec and _spec.loader
spec_tests = importlib.util.module_from_spec(_spec)
sys.modules["spec_tests"] = spec_tests
_spec.loader.exec_module(spec_tests)

# The items of each section, in the spec's order.
SECTIONS = {
    "security": (
        "§63",
        [
            "bola",
            "cross-tenant-reads",
            "api-key-scopes",
            "expired-revoked-key",
            "approval-replay",
            "approval-tampering",
            "sql-injection",
            "xss-trace-content",
            "malicious-markdown",
            "ssrf",
            "oversized-payload",
            "malicious-openapi",
            "malicious-mcp",
            "secret-redaction",
            "prompt-injection",
            "poison-message",
            "path-traversal",
        ],
    ),
    "chaos": (
        "§64",
        [
            "rabbitmq-unavailable",
            "duplicate-message",
            "delayed-message",
            "db-connection-loss",
            "worker-crash",
            "llm-429",
            "llm-timeout",
            "malformed-provider-json",
            "tool-twin-crash",
            "evaluator-exception",
        ],
    ),
}


@pytest.mark.parametrize("suite", sorted(SECTIONS))
def test_every_item_of_the_spec_is_in_its_manifest_once(suite: str) -> None:
    manifest = spec_tests.MANIFESTS[suite]
    spec, ids = SECTIONS[suite]
    assert spec_tests.spec_of(manifest) == spec
    assert [item.id for item in spec_tests.load(manifest)] == ids


@pytest.mark.parametrize("suite", sorted(SECTIONS))
def test_every_named_test_exists(suite: str) -> None:
    assert spec_tests.missing(spec_tests.load(spec_tests.MANIFESTS[suite])) == []


def test_a_missing_test_is_reported(tmp_path: Path) -> None:
    manifest = tmp_path / "m.yaml"
    manifest.write_text(
        "spec: X\nitems:\n"
        "  - id: x\n    title: X\n"
        "    go: {packages/gokit/textx: [TestValidAndClean, TestNoSuchThing]}\n"
        "    python: {packages/core-py/tests/test_web.py: [test_storable, test_no_such_thing]}\n"
        "    web: [tests/unit/no-such.test.ts]\n"
        "  - id: y\n    title: Y\n"
    )
    problems = spec_tests.missing(spec_tests.load(manifest))
    assert problems == [
        "x: packages/gokit/textx: no func TestNoSuchThing",
        "x: packages/core-py/tests/test_web.py: no def test_no_such_thing",
        "x: no web test file tests/unit/no-such.test.ts",
        "y: no tests",
    ]
