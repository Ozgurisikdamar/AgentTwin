"""The security manifest (scripts/security-tests.yaml) covers every item of
spec §63 with tests that exist: a renamed or deleted test must not leave an
item silently uncovered (``make test-security`` runs them)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("security_tests", ROOT / "scripts" / "security_tests.py")
assert _spec and _spec.loader
security_tests = importlib.util.module_from_spec(_spec)
sys.modules["security_tests"] = security_tests
_spec.loader.exec_module(security_tests)

# Spec §63, in its order.
SPEC_63 = [
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
]


def test_every_item_of_the_spec_is_in_the_manifest_once() -> None:
    ids = [item.id for item in security_tests.load()]
    assert ids == SPEC_63


def test_every_named_test_exists() -> None:
    assert security_tests.missing(security_tests.load()) == []


def test_a_missing_test_is_reported(tmp_path: Path) -> None:
    manifest = tmp_path / "m.yaml"
    manifest.write_text(
        "items:\n"
        "  - id: x\n    title: X\n"
        "    go: {packages/gokit/textx: [TestValidAndClean, TestNoSuchThing]}\n"
        "    python: {packages/core-py/tests/test_web.py: [test_storable, test_no_such_thing]}\n"
        "    web: [tests/unit/no-such.test.ts]\n"
        "  - id: y\n    title: Y\n"
    )
    problems = security_tests.missing(security_tests.load(manifest))
    assert problems == [
        "x: packages/gokit/textx: no func TestNoSuchThing",
        "x: packages/core-py/tests/test_web.py: no def test_no_such_thing",
        "x: no web test file tests/unit/no-such.test.ts",
        "y: no tests",
    ]
