"""The image policy of scripts/supply_chain.py, on trivy report shapes."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from supply_chain import Finding, findings, suppressed, verdict, violations


def vuln(vid: str, severity: str, fixed: str = "", pkg: str = "libfoo") -> dict[str, Any]:
    return {
        "VulnerabilityID": vid,
        "Severity": severity,
        "PkgName": pkg,
        "InstalledVersion": "1.0",
        "FixedVersion": fixed,
    }


def report(*vulns: dict[str, Any], target: str = "img (debian 13)") -> dict[str, Any]:
    return {"Results": [{"Target": target, "Vulnerabilities": list(vulns)}]}


def ids(found: list[Finding]) -> list[str]:
    return [f.vulnerability for f in found]


def test_our_images_fail_on_a_high_that_has_a_fix() -> None:
    found = findings(report(vuln("CVE-1", "HIGH", fixed="1.1"), vuln("CVE-2", "HIGH")))
    assert ids(violations("agenttwin/web:dev", found)) == ["CVE-1"]


def test_third_party_images_only_fail_on_critical() -> None:
    found = findings(report(vuln("CVE-1", "HIGH", fixed="1.1"), vuln("CVE-2", "MEDIUM", fixed="2")))
    assert violations("grafana/grafana:12.4.11", found) == []


def test_a_critical_fails_every_image_fixed_or_not() -> None:
    found = findings(report(vuln("CVE-9", "CRITICAL")))
    assert ids(violations("rabbitmq:4.3.6-management-alpine", found)) == ["CVE-9"]
    assert ids(violations("agenttwin/postgres:dev", found)) == ["CVE-9"]


def test_lower_severities_never_fail() -> None:
    found = findings(report(vuln("CVE-3", "MEDIUM", fixed="1"), vuln("CVE-4", "LOW", fixed="1")))
    assert violations("agenttwin/control-plane:dev", found) == []


def test_a_package_reported_by_several_targets_counts_once() -> None:
    data = {
        "Results": [
            {"Target": "img", "Vulnerabilities": [vuln("CVE-5", "HIGH", fixed="1.1")]},
            {"Target": "img", "Vulnerabilities": [vuln("CVE-5", "HIGH", fixed="1.1")]},
        ]
    }
    assert ids(findings(data)) == ["CVE-5"]


def test_an_empty_or_clean_report_passes() -> None:
    assert verdict("agenttwin/web:dev", {}).violations == []
    no_vulns = {"Results": [{"Target": "x", "Vulnerabilities": None}]}
    assert verdict("agenttwin/web:dev", no_vulns).violations == []


def test_the_verdict_counts_by_severity_and_fix() -> None:
    v = verdict(
        "agenttwin/demo:dev",
        report(vuln("A", "HIGH", fixed="1"), vuln("B", "HIGH"), vuln("C", "HIGH", pkg="libbar")),
    )
    assert (v.count("HIGH", fixable=True), v.count("HIGH", fixable=False)) == (1, 2)
    assert ids(v.violations) == ["A"]


def test_exceptions_the_ignore_file_applied_are_listed() -> None:
    data = report(vuln("CVE-7", "HIGH"))
    data["Results"][0]["ExperimentalModifiedFindings"] = [
        {"Status": "ignored", "Statement": "not reachable", "Finding": {"VulnerabilityID": "CVE-2026-6653"}},
    ]
    assert suppressed(data) == [("CVE-2026-6653", "not reachable")]
