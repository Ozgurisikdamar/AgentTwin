"""The text a scenario is embedded from."""

from __future__ import annotations

import json
from pathlib import Path

from agenttwin_core.embeddings import MAX_TEXT_CHARS
from agenttwin_core.yamlsafe import load_yaml
from agenttwin_simulation.matching import SCENARIO_SOURCES, scenario_text

SCENARIOS = Path(__file__).resolve().parents[3] / "demo" / "support-refund-agent" / "assurance" / "scenarios"


def test_the_text_says_what_a_scenario_is_about() -> None:
    doc = load_yaml((SCENARIOS / "refund-timeout-after-mutation.yaml").read_text())
    lines = scenario_text(doc).splitlines()
    assert lines[0] == "refund timeout after mutation"
    assert lines[1].startswith("The payment provider applies the refund but its response times out.")
    assert "refunds faults idempotency" in lines
    # What it covers, without the kind of the reference.
    assert "refund_payment timeout_after_mutation" in lines
    assert "Hi! One item in ORD-1001 arrived broken. Can I get a refund of $40?" in lines
    # The fault: its target and behavior, not its message or timing.
    assert "refund_payment timeout_after_mutation" in lines
    assert "The payment provider did not answer in time." not in scenario_text(doc)
    # Expectations: id, type, description and the tools they name.
    assert "no-double-refund noDuplicateSideEffect refund_payment" in lines
    assert "refunded-exactly-once state" in lines
    # Values of state checks are not words about the scenario.
    assert "orders.ORD-1001" not in scenario_text(doc)


def test_the_text_does_not_depend_on_what_is_irrelevant() -> None:
    doc = load_yaml((SCENARIOS / "refund-happy-path.yaml").read_text())
    other = load_yaml((SCENARIOS / "refund-happy-path.yaml").read_text())
    other["metadata"]["owner"] = "someone-else"
    other["metadata"]["severity"] = "low"
    other["spec"]["seed"] = 99
    other["spec"]["input"]["context"] = {"tenant": "x"}
    assert scenario_text(doc) == scenario_text(other)


def test_malformed_documents_give_what_they_have() -> None:
    assert scenario_text({}) == ""
    assert (
        scenario_text({"metadata": {"name": "a-b", "tags": ["x", 3]}, "spec": {"covers": "tool:t"}})
        == "a b\nx\nt"
    )
    doc = {
        "metadata": {"name": "n", "description": ["not", "text"]},
        "spec": {
            "faults": ["x", {"target": 5, "behavior": {"type": "delay"}}],
            "expectations": [7, {"id": "e"}],
        },
    }
    assert scenario_text(doc) == "n\nnot\ntext\ndelay\ne"


def test_the_text_is_bounded() -> None:
    doc = {"metadata": {"name": "n", "description": "word " * MAX_TEXT_CHARS}, "spec": {}}
    assert len(scenario_text(doc)) == MAX_TEXT_CHARS


def test_sources_are_the_documents_enum() -> None:
    schema = Path(__file__).resolve().parents[3] / "packages/scenario-schema/schemas/scenario.v1.schema.json"
    enum = json.loads(schema.read_text())["properties"]["metadata"]["properties"]["source"]["enum"]
    assert list(SCENARIO_SOURCES) == enum
