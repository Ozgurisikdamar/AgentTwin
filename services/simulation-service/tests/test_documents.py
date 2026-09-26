"""Scenario/twin documents: parsing, validation and the scenario↔twin checks."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from agenttwin_core.evaluators import default_registry
from agenttwin_core.yamlsafe import load_yaml
from agenttwin_simulation.documents import (
    DocumentProblem,
    check_scenario,
    export_yaml,
    parse_document,
    scenario_twin_problems,
    spec_hash,
    validate_twin,
)

ASSURANCE = Path(__file__).resolve().parents[3] / "demo" / "support-refund-agent" / "assurance"
TWIN = load_yaml((ASSURANCE / "twin.yaml").read_text())
HAPPY = load_yaml((ASSURANCE / "scenarios" / "refund-happy-path.yaml").read_text())
REGISTRY = default_registry()


def scenario(**spec_changes: Any) -> dict[str, Any]:
    doc = copy.deepcopy(HAPPY)
    doc["spec"].update(spec_changes)
    return doc


def test_parse_json_and_yaml_documents() -> None:
    text = (ASSURANCE / "scenarios" / "refund-happy-path.yaml").read_text()
    assert parse_document({"yaml": text}) == parse_document({"document": HAPPY}) == HAPPY
    # The export round-trips.
    assert parse_document({"yaml": export_yaml(HAPPY)}) == HAPPY


def jsonb_order(value: Any) -> Any:
    """Keys the way PostgreSQL's jsonb returns them: shorter keys first."""
    if isinstance(value, dict):
        return {k: jsonb_order(value[k]) for k in sorted(value, key=lambda k: (len(k), k))}
    if isinstance(value, list):
        return [jsonb_order(v) for v in value]
    return value


@pytest.mark.parametrize("name", ["refund-timeout-after-mutation", "twin"])
def test_export_follows_the_reading_order_not_the_storage_order(name: str) -> None:
    path = ASSURANCE / ("twin.yaml" if name == "twin" else f"scenarios/{name}.yaml")
    authored = load_yaml(path.read_text())
    stored = jsonb_order(authored)
    assert list(stored) == ["kind", "spec", "metadata", "apiVersion"]
    exported = export_yaml(stored)
    assert load_yaml(exported) == authored
    top = [line.split(":")[0] for line in exported.splitlines() if line and not line.startswith(" ")]
    assert top == ["apiVersion", "kind", "metadata", "spec"]
    doc = load_yaml(exported)
    assert next(iter(doc["metadata"])) == "name"
    if name == "twin":
        assert list(doc["spec"]) == ["tenantKey", "secrets", "retrieval", "initialState", "tools"]
        return
    assert list(doc["spec"]) == ["agent", "twin", "covers", "input", "faults", "expectations"]
    assert list(doc["spec"]["input"]) == ["message", "context"]
    assert list(doc["spec"]["faults"][0]) == ["target", "when", "behavior"]
    assert next(iter(doc["spec"]["faults"][0]["behavior"])) == "type"
    for exp in doc["spec"]["expectations"]:
        keys = list(exp)
        assert keys[:2] == ["id", "type"]
        assert "critical" not in keys[:-1]


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ({}, "exactly one"),
        ({"yaml": "a: 1", "document": {}}, "exactly one"),
        ({"yaml": 5}, "must be a string"),
        ({"yaml": "- a\n- b"}, "must be an object"),
        ({"yaml": "a: [1,"}, "invalid YAML (line"),
        ({"yaml": "x" * (600 * 1024)}, "larger than"),
        ({"document": [1, 2]}, "must be an object"),
    ],
)
def test_parse_rejects_unusable_bodies(body: dict[str, Any], problem: str) -> None:
    with pytest.raises(DocumentProblem) as err:
        parse_document(body)
    assert problem in str(err.value)


def test_yaml_parsing_is_safe() -> None:
    # Arbitrary Python object tags are not constructed.
    with pytest.raises(DocumentProblem):
        parse_document({"yaml": "a: !!python/object/apply:os.system ['true']"})


def test_spec_hash_ignores_key_order_but_not_content() -> None:
    reordered = {k: HAPPY[k] for k in reversed(list(HAPPY))}
    assert spec_hash(reordered) == spec_hash(HAPPY)
    changed = scenario(input={"message": "Something else", "context": {"tenant": "demo-co"}})
    assert spec_hash(changed) != spec_hash(HAPPY)


def test_demo_scenarios_are_valid() -> None:
    twin = validate_twin(TWIN)
    for path in sorted((ASSURANCE / "scenarios").glob("*.yaml")):
        doc = load_yaml(path.read_text())
        check = check_scenario(doc, REGISTRY)
        assert check.valid, (path.name, check.problems)
        cross = scenario_twin_problems(doc, twin)
        assert cross.valid and not cross.warnings, (path.name, cross.problems, cross.warnings)


def test_check_scenario_reports_semantic_problems() -> None:
    doc = scenario(
        expectations=[
            {"type": "outputRegex", "pattern": "(unclosed"},
            {"type": "state", "path": "orders..x", "equals": 1},
            {"type": "outputJsonSchema", "schema": {"type": "nope"}},
        ],
        faults=[{"target": "refund_payment", "behavior": {"type": "semantic_bad_response"}}],
        simulationMode="approximate",
    )
    check = check_scenario(doc, REGISTRY)
    text = "\n".join(check.problems)
    assert "spec.expectations[0]: pattern is not a valid regular expression" in text
    assert "spec.expectations[1]: path:" in text
    assert "spec.expectations[2]: schema:" in text
    assert "semantic_bad_response needs a body" in text
    assert "approximate (LLM-simulated) tools are not enabled" in text


def test_check_scenario_schema_errors_and_twin_requirement() -> None:
    bad = scenario()
    bad["metadata"]["severity"] = "urgent"
    assert any("severity" in p for p in check_scenario(bad, REGISTRY).problems)
    no_twin = scenario()
    del no_twin["spec"]["twin"]
    check = check_scenario(no_twin, REGISTRY)
    assert any("needs a tool twin" in p for p in check.problems)
    no_agent = scenario()
    del no_agent["spec"]["agent"]
    assert any("every agent of the project" in w for w in check_scenario(no_agent, REGISTRY).warnings)
    semantic = scenario(expectations=[{"type": "semantic", "rubric": "polite"}])
    assert any("evaluation run's judge" in w for w in check_scenario(semantic, REGISTRY).warnings)


def test_scenario_twin_cross_checks() -> None:
    twin = validate_twin(TWIN)
    doc = scenario(
        allowedTools=["lookup_order", "wire_transfer"],
        faults=[{"target": "wire_transfer", "behavior": {"type": "http_500"}}],
        expectations=[
            {"type": "toolCalled", "tool": "cancel_order"},
            {"type": "toolNotCalled", "tool": "nuke"},
        ],
    )
    check = scenario_twin_problems(doc, twin)
    assert "spec.faults[0].target: the twin has no tool 'wire_transfer'" in check.problems
    assert "spec.allowedTools: the twin 'demo-co-support' has no tool 'wire_transfer'" in check.problems
    # A tool the scenario expects to be called but the twin lacks is a warning;
    # "must not be called" about an unknown tool is trivially true.
    assert check.warnings == ["spec.expectations[0]: the twin 'demo-co-support' has no tool 'cancel_order'"]
    huge = scenario(state={"blob": "x" * (1 << 20)})
    assert any("larger than" in p for p in scenario_twin_problems(huge, twin).problems)


def test_validate_twin_reports_problems() -> None:
    bad = copy.deepcopy(TWIN)
    bad["spec"]["tools"]["refund_payment"]["handler"]["path"] = "orders.{order_id"
    with pytest.raises(DocumentProblem) as err:
        validate_twin(bad)
    assert any("refund_payment" in p for p in err.value.problems)
