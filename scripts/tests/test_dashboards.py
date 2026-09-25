"""The Grafana dashboards (spec §54) are provisioned from infra/grafana and
query only metrics the services actually define: a renamed metric breaks a
panel silently in Grafana, so it breaks this test instead."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DASHBOARDS = sorted((ROOT / "infra" / "grafana" / "dashboards").glob("*.json"))
DATASOURCES = ROOT / "infra" / "grafana" / "provisioning" / "datasources" / "prometheus.yml"
PROMETHEUS = ROOT / "infra" / "prometheus" / "prometheus.yml"

# Metrics other processes export: the collector, RabbitMQ and the client
# libraries' process collectors.
EXTERNAL = ("otelcol_", "rabbitmq_", "process_")
METRIC = re.compile(r"\b((?:agenttwin|otelcol|rabbitmq|process)_[a-z0-9_]+)\b")
SUFFIXES = ("_bucket", "_count", "_sum")


def load(path: Path) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(path.read_text())
    return doc


def panels(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in doc["panels"] if p["type"] != "row"]


def defined_metrics() -> set[str]:
    """Every agenttwin_* name written as a string literal in service code."""
    names: set[str] = set()
    for pattern in ("services/**/*.go", "packages/**/*.go", "services/**/*.py", "packages/**/*.py"):
        for path in ROOT.glob(pattern):
            if path.name.endswith("_test.go") or "/tests/" in str(path) or "node_modules" in path.parts:
                continue
            names.update(re.findall(r"\"(agenttwin_[a-z0-9_]+)\"", path.read_text(errors="ignore")))
    return names


def base_name(metric: str) -> str:
    for suffix in SUFFIXES:
        if metric.endswith(suffix):
            return metric[: -len(suffix)]
    return metric


def test_there_are_dashboards_and_their_uids_are_unique() -> None:
    uids = [load(p)["uid"] for p in DASHBOARDS]
    assert len(DASHBOARDS) >= 3
    assert len(set(uids)) == len(uids)
    for path in DASHBOARDS:
        assert path.stem == load(path)["uid"], "the file is named after its uid"


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_every_panel_queries_the_provisioned_datasource_and_says_what_it_shows(path: Path) -> None:
    uid = yaml.safe_load(DATASOURCES.read_text())["datasources"][0]["uid"]
    doc = load(path)
    assert "agenttwin" in doc["tags"] and doc["description"]
    ids = [p["id"] for p in doc["panels"]]
    assert len(set(ids)) == len(ids)
    for p in panels(doc):
        # A chart's description is its textual summary (spec §42).
        assert p["description"].strip(), p["title"]
        assert p["datasource"]["uid"] == uid, p["title"]
        assert p["targets"], p["title"]
        for t in p["targets"]:
            assert t["datasource"]["uid"] == uid and t["expr"].strip(), p["title"]


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_every_metric_a_panel_queries_is_defined(path: Path) -> None:
    defined = defined_metrics()
    missing = set()
    for p in panels(load(path)):
        for t in p["targets"]:
            for metric in METRIC.findall(t["expr"]):
                if metric.startswith(EXTERNAL):
                    continue
                if base_name(metric) not in defined:
                    missing.add((p["title"], metric))
    assert not missing, f"panels query metrics no service defines: {sorted(missing)}"


def test_the_code_scan_sees_the_metrics_it_should() -> None:
    # Guards the scan itself: were it to find nothing, every panel would fail
    # loudly; were it too loose, a typo could pass. Spot-check both ways.
    defined = defined_metrics()
    expected = {
        "agenttwin_http_requests_total",
        "agenttwin_eval_runs_total",
        "agenttwin_runtime_decisions_total",
    }
    assert expected <= defined
    assert "agenttwin_no_such_metric" not in defined


def test_rabbitmq_queue_panels_have_their_scrape_job() -> None:
    exprs = [t["expr"] for path in DASHBOARDS for p in panels(load(path)) for t in p["targets"]]
    uses_detailed = any("rabbitmq_detailed_" in e for e in exprs)
    jobs = {j["job_name"]: j for j in yaml.safe_load(PROMETHEUS.read_text())["scrape_configs"]}
    assert uses_detailed
    job = jobs["rabbitmq-queues"]
    assert job["metrics_path"] == "/metrics/detailed" and job["params"]["family"] == ["queue_coarse_metrics"]
