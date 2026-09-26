"""The agent manifest helper: what instrumentation reads from a manifest."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agenttwin.hashing import sha256_hex
from agenttwin.manifest import load_manifest, parse_manifest

REPO = Path(__file__).resolve().parents[3]
DEMO = REPO / "demo" / "support-refund-agent" / "manifests"


@pytest.mark.parametrize("path", sorted(DEMO.glob("*.yaml")), ids=lambda p: p.name)
def test_reads_the_demo_manifests(path: Path) -> None:
    doc = yaml.safe_load(path.read_text())
    m = load_manifest(path)
    assert (m.name, m.version) == ("support-refund-agent", doc["metadata"]["version"])
    assert m.prompt_hash == sha256_hex(doc["spec"]["instructions"])
    assert m.risk_of("lookup_order") == "READ"
    assert m.risk_of("refund_payment") == "WRITE_IRREVERSIBLE"  # risk: {level: ...}


def test_the_prompt_of_a_prompt_ref_manifest_is_the_references_hash() -> None:
    base = {"apiVersion": "agenttwin.dev/v1", "kind": "Agent", "metadata": {"name": "a", "version": "1"}}
    ref = parse_manifest({**base, "spec": {"promptRef": {"name": "p", "sha256": "ab" * 32}}})
    assert ref.prompt_hash == "ab" * 32
    assert parse_manifest(base).prompt_hash is None
    with pytest.raises(ValueError):
        parse_manifest({**base, "kind": "Scenario"})
