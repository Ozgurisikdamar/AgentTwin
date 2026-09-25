"""The payload builders the client tests' fakes answer with are what the
simulation API's contract describes, and the exchange checker keeps what it
finds (ADR-0021)."""

from __future__ import annotations

import pytest

from agenttwin_core import simulation_fakes as fake
from agenttwin_core.openapi_contract import Contract, ContractViolation, contract_path


@pytest.fixture(scope="module")
def contract() -> Contract:
    return Contract.load(contract_path("simulation-service"))


def test_builders_are_valid_contract_payloads(contract: Contract) -> None:
    contract.check_schema("Twin", fake.twin())
    contract.check_schema("TwinSummary", fake.twin_summary(description="Support tools."))
    contract.check_schema("Scenario", fake.scenario(tags=["smoke"]))
    for status in ("QUEUED", "RUNNING", "EVALUATING", "COMPLETED", "FAILED", "CANCELLED"):
        contract.check_schema("PinnedRun", fake.run(status=status))
    contract.check_schema("QueuedCase", fake.queued_case(3, "refund-happy-path"))
    cases = [
        fake.case_summary(i, f"s{i}", status)
        for i, status in enumerate(("PENDING", "RUNNING", "PASSED", "FAILED", "ERRORED", "CANCELLED"))
    ]
    contract.check_schema("RunDetail", fake.run_detail(fake.run(status="RUNNING"), cases))
    contract.check_schema("Error", fake.error("SCENARIO_INVALID", "invalid", problems=["x"]))
    # Overrides are checked like everything else.
    with pytest.raises(ContractViolation, match="'SOMETIMES' is not one of"):
        contract.check_schema("PinnedRun", fake.run(status="SOMETIMES"))


def test_the_checker_keeps_violations_and_counts_only_what_passed() -> None:
    checker = fake.ExchangeChecker()
    run = f"/api/v1/simulations/{fake.uuid(1)}"
    checker.check("GET", "/api/v1/projects", {}, b"", 200, {"not": "checked"})  # another service
    checker.check("GET", run, {}, b"", 200, {"run": {}})
    assert len(checker.violations) == 1 and "getSimulation 200 response" in checker.violations[0]
    assert checker.succeeded() == set()  # a failed check is not coverage
    checker.check("GET", f"{run}?verbose=1", {}, b"", 200, fake.run_detail(fake.run(), []))
    assert "undocumented query parameter 'verbose'" in checker.violations[1]
    checker.check("GET", run, {}, b"", 200, fake.run_detail(fake.run(), []))
    assert checker.succeeded() == {"getSimulation"} and len(checker.violations) == 2
