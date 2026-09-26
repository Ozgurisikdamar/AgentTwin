"""The alert rules (infra/prometheus/alerts.yml) are loaded by Prometheus, read
only metrics the services and the infrastructure actually export, and each
says how bad it is and where its runbook is. What each rule does is tested
with promtool (`make alerts-check`)."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_dashboards import EXTERNAL, METRIC, base_name, defined_metrics

ROOT = Path(__file__).resolve().parents[2]
PROMETHEUS = ROOT / "infra" / "prometheus"
COMPOSE = ROOT / "docker-compose.yml"
RUNBOOK = re.compile(r"^docs/runbooks/[a-z0-9-]+\.md(#[a-z0-9-]+)?$|^docs/runbooks/README\.md(#[a-z0-9-]+)?$")


def rules() -> list[dict[str, Any]]:
    doc = yaml.safe_load((PROMETHEUS / "alerts.yml").read_text())
    return [r for g in doc["groups"] for r in g["rules"]]


def test_prometheus_loads_the_rules_and_compose_mounts_them() -> None:
    config = yaml.safe_load((PROMETHEUS / "prometheus.yml").read_text())
    assert config["rule_files"] == ["alerts.yml"]
    prometheus = yaml.safe_load(COMPOSE.read_text())["services"]["prometheus"]
    assert "./infra/prometheus/alerts.yml:/etc/prometheus/alerts.yml:ro" in prometheus["volumes"]


def test_every_alert_has_a_severity_and_a_runbook() -> None:
    names = [r["alert"] for r in rules()]
    assert len(names) >= 10 and len(set(names)) == len(names)
    for r in rules():
        assert r["labels"]["severity"] in {"critical", "warning"}, r["alert"]
        assert r["for"], r["alert"]
        for key in ("summary", "description", "runbook"):
            assert r["annotations"].get(key), f"{r['alert']}: no {key}"
        assert RUNBOOK.match(r["annotations"]["runbook"]), r["alert"]


def test_the_rules_read_only_metrics_that_exist() -> None:
    ours = defined_metrics()
    for r in rules():
        for metric in METRIC.findall(r["expr"]):
            if metric.startswith(EXTERNAL):
                continue
            assert base_name(metric) in ours, f"{r['alert']} reads {metric}, which no service exports"


def test_rabbitmq_queue_metrics_the_rules_read_are_scraped() -> None:
    config = yaml.safe_load((PROMETHEUS / "prometheus.yml").read_text())
    job = next(j for j in config["scrape_configs"] if j["job_name"] == "rabbitmq-queues")
    families = set(job["params"]["family"])
    wanted = {"rabbitmq_detailed_queue_messages": "queue_coarse_metrics"}
    wanted["rabbitmq_detailed_queue_messages_ready"] = "queue_coarse_metrics"
    wanted["rabbitmq_detailed_queue_consumers"] = "queue_consumer_count"
    read = {m for r in rules() for m in METRIC.findall(r["expr"]) if m.startswith("rabbitmq_detailed_")}
    assert read <= set(wanted), read - set(wanted)
    for metric in read:
        assert wanted[metric] in families, f"{metric} needs the {wanted[metric]} family"


def test_every_alert_has_a_promtool_test() -> None:
    tests = yaml.safe_load((PROMETHEUS / "alerts.test.yml").read_text())
    tested = {c["alertname"] for t in tests["tests"] for c in t.get("alert_rule_test", [])}
    assert {r["alert"] for r in rules()} <= tested
