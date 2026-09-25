"""Command line entry points for the demo.

support-refund-agent serve-tools   Demo Co tools over HTTP (production tools)
support-refund-agent serve-agent   the agent adapter HTTP server
support-refund-agent run TEXT      one conversation; prints the result and trace id
support-refund-agent traffic       production-like traffic (optionally verified)
support-refund-agent seed          load the demo workspace: register the agent
                                   manifests, import the tool catalogs, register the
                                   tool twin and the scenarios, run simulations, keep
                                   the regression suite (a dataset) and evaluate a
                                   candidate against its baseline on it, compare
                                   versions (change sets, with the scenarios each
                                   change requires) and send verified production
                                   traffic
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from agenttwin import AgentTwin, APIError, Client, Config
from support_refund_agent.agent import Agent, ManifestStore, RunRequest, scripted_model_factory
from support_refund_agent.server import AgentServer
from support_refund_agent.tools_server import ToolsServer, parse_faults
from support_refund_agent.traffic import TrafficGenerator
from support_refund_agent.world import INTERNAL_API_KEY

DEFAULT_TOOLS_URL = "http://127.0.0.1:8091"
# The dataset the seed keeps: every scenario of the demo's assurance directory.
SUITE = "refund-regression-suite"


def _telemetry() -> AgentTwin:
    return AgentTwin(Config.from_env(service_name="support-refund-agent"))


def _model_factory() -> Any:
    kind = os.environ.get("DEMO_AGENT_MODEL", "scripted")
    if kind == "anthropic":
        from support_refund_agent.anthropic_model import AnthropicModel

        model_name = os.environ.get("DEMO_AGENT_ANTHROPIC_MODEL", "claude-sonnet-5")

        def make(manifest: Any, req: RunRequest) -> AnthropicModel:
            return AnthropicModel(model_name, api_key=os.environ.get("ANTHROPIC_API_KEY"))

        return make
    if kind != "scripted":
        raise SystemExit(f"DEMO_AGENT_MODEL must be scripted or anthropic, got {kind!r}")
    return scripted_model_factory(os.environ.get("DEMO_AGENT_INTERNAL_API_KEY", INTERNAL_API_KEY))


def _agent(telemetry: AgentTwin, tools_url: str) -> Agent:
    return Agent(
        telemetry,
        ManifestStore(),
        tools_base_url=tools_url,
        model_factory=_model_factory(),
        tool_timeout_s=float(os.environ.get("DEMO_AGENT_TOOL_TIMEOUT_S", "1.5")),
    )


def _serve(stop: Any) -> None:
    signal.signal(signal.SIGTERM, lambda *_: stop())
    try:
        signal.pause()
    except KeyboardInterrupt:
        pass
    finally:
        stop()


def cmd_serve_tools(args: argparse.Namespace) -> int:
    faults = parse_faults(args.faults or os.environ.get("DEMO_TOOLS_FAULTS"))
    server = ToolsServer(
        host=args.host,
        port=args.port,
        faults=faults,
        seed=args.seed,
        admin_token=os.environ.get("DEMO_TOOLS_ADMIN_TOKEN"),
    ).start()
    logging.info(
        "demo tools listening on %s (faults: %s)",
        server.url,
        [f"{f.tool}:{f.kind}:{f.probability}" for f in faults],
    )
    _serve(server.stop)
    return 0


def cmd_serve_agent(args: argparse.Namespace) -> int:
    telemetry = _telemetry()
    agent = _agent(telemetry, args.tools_url)
    server = AgentServer(
        agent, host=args.host, port=args.port, token=os.environ.get("DEMO_AGENT_TOKEN")
    ).start()
    logging.info("demo agent listening on %s (versions %s)", server.url, agent.manifests.versions)

    def stop() -> None:
        server.stop()
        telemetry.shutdown()

    _serve(stop)
    return 0


def _http_run(agent_url: str, req: RunRequest) -> dict[str, Any]:
    body = {
        "input": req.input,
        "customer_id": req.customer_id,
        "tenant": req.tenant,
        "agent_version": req.version,
        "run_context": {"source": req.source, "environment": req.environment},
    }
    headers = {"Content-Type": "application/json"}
    if token := os.environ.get("DEMO_AGENT_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    http_req = urllib.request.Request(  # noqa: S310 - URL from the operator's configuration
        agent_url.rstrip("/") + "/run", data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(http_req, timeout=60) as resp:  # noqa: S310
        result: dict[str, Any] = json.loads(resp.read())
        return result


def _create_order(tools_url: str, customer_id: str, total: float) -> str:
    """Creates a fresh delivered order through the tools' admin API so that a
    demo refund never collides with earlier runs."""
    token = os.environ.get("DEMO_TOOLS_ADMIN_TOKEN")
    if not token:
        raise SystemExit("--new-order needs DEMO_TOOLS_ADMIN_TOKEN")
    req = urllib.request.Request(  # noqa: S310 - URL from the operator's configuration
        tools_url.rstrip("/") + "/admin/orders",
        data=json.dumps(
            {"customer_id": customer_id, "total": total, "status": "delivered", "age_days": 3}
        ).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        order_id: str = json.loads(resp.read())["order_id"]
        return order_id


def cmd_run(args: argparse.Namespace) -> int:
    text = args.text
    if args.new_order is not None:
        order_id = _create_order(args.tools_url, args.customer, args.new_order)
        text = text.replace("{order}", order_id) if "{order}" in text else f"{text} (order {order_id})"
    req = RunRequest(input=text, customer_id=args.customer, version=args.version, source="production")
    if args.agent_url:
        result = _http_run(args.agent_url, req)
    else:
        telemetry = _telemetry()
        result = _agent(telemetry, args.tools_url).run(req).to_json()
        telemetry.shutdown()
    print(json.dumps(result, indent=2))
    ui = os.environ.get("AGENTTWIN_UI_URL")
    if ui:
        print(f"\nTrace: {ui.rstrip('/')}/traces/{result['trace_id']}")
    return 0


def _traffic(args: argparse.Namespace, cfg: Config, *, verify_outcomes: bool) -> list[dict[str, Any]]:
    """Runs ``args.count`` conversations, over HTTP against a deployed agent
    (``--agent-url``) or in process."""
    telemetry = None
    run: Callable[[RunRequest], dict[str, Any]]
    if args.agent_url:
        agent_url = args.agent_url

        def run(req: RunRequest) -> dict[str, Any]:
            return _http_run(agent_url, req)
    else:
        telemetry = _telemetry()
        agent = _agent(telemetry, args.tools_url)

        def run(req: RunRequest) -> dict[str, Any]:
            return agent.run(req).to_json()

    def flush() -> None:
        if telemetry is not None:
            telemetry.flush()

    gen = TrafficGenerator(
        run,
        tools_url=args.tools_url,
        tools_admin_token=os.environ.get("DEMO_TOOLS_ADMIN_TOKEN"),
        versions=_parse_versions(args.versions),
        seed=args.seed,
        telemetry_config=cfg,
        verify_outcomes=verify_outcomes,
        flush=flush,
    )
    try:
        return gen.run(args.count, delay_s=getattr(args, "delay", 0.0))
    finally:
        if telemetry is not None:
            telemetry.shutdown()


def cmd_traffic(args: argparse.Namespace) -> int:
    records = _traffic(args, Config.from_env(), verify_outcomes=args.verify_outcomes)
    summary: dict[str, int] = {}
    for r in records:
        key = f"{r['version']}:{r['kind']}:{r.get('business_outcome')}"
        summary[key] = summary.get(key, 0) + 1
    print(
        json.dumps(
            {"conversations": len(records), "by_version_kind_outcome": summary}, indent=2, sort_keys=True
        )
    )
    return 0 if len(records) == args.count else 1


def default_assurance_dir() -> Path:
    """The demo's tool twin and scenarios (registered by ``seed``)."""
    env = os.environ.get("DEMO_ASSURANCE_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "assurance"


def _register_assurance(client: Client, project_id: str, directory: Path) -> dict[str, Any]:
    """Registers the tool twin, then every scenario (a scenario names its
    twin, so the twin goes first). Unchanged content is a no-op on the
    server; changed content becomes a new version."""
    scenario_files = sorted((directory / "scenarios").glob("*.yaml"))
    if not scenario_files:
        raise FileNotFoundError(f"no scenarios in {directory / 'scenarios'}")
    res = client.register_twin(project_id, (directory / "twin.yaml").read_text(encoding="utf-8"))
    twin = res.get("twin") or {}
    twin_summary = {
        "name": twin.get("name"),
        "version": twin.get("version"),
        "created": bool(res.get("created")),
    }
    logging.info(
        "tool twin %s v%s %s",
        twin_summary["name"],
        twin_summary["version"],
        "registered" if twin_summary["created"] else "unchanged",
    )
    saved: list[str] = []
    unchanged: list[str] = []
    for path in scenario_files:
        res = client.save_scenario(project_id, path.read_text(encoding="utf-8"))
        name = str((res.get("scenario") or {}).get("name") or path.stem)
        (saved if res.get("created") else unchanged).append(name)
    logging.info("scenarios: %d saved, %d unchanged", len(saved), len(unchanged))
    return {"twin": twin_summary, "scenarios": {"saved": saved, "unchanged": unchanged}}


def _ensure_suite(client: Client, project_id: str, scenarios: Sequence[str]) -> dict[str, Any]:
    """The regression suite with every registered scenario: created the first
    time, a new version when scenarios were added (unchanged otherwise)."""
    found = client.datasets(project_id, name=SUITE)
    if found:
        detail = client.add_dataset_cases(str(found[0]["id"]), scenarios, note="seeded")
    else:
        detail = client.create_dataset(
            project_id,
            SUITE,
            scenarios,
            description="Every scenario of the support refund agent (loaded by the demo seed).",
            tags=["demo"],
        )
    dataset, version = detail.get("dataset") or {}, detail.get("version") or {}
    logging.info(
        "dataset %s v%s (%d cases)", dataset.get("name"), version.get("version"), version.get("case_count", 0)
    )
    return {"id": dataset.get("id"), "name": dataset.get("name"), "version": version.get("version")}


def _import_catalogs(client: Client, project_id: str, directory: Path) -> list[dict[str, Any]]:
    """Imports the tool catalogs ``imports.yaml`` lists (none without it):
    the OpenAPI documents and MCP servers behind the agent's tools. The
    manifests are registered first, so the tools they declare keep the
    manifests' definitions; the graph links them to the imported APIs. An
    unchanged document stores nothing."""
    openapi, mcp = _read_imports(directory)
    out = [_catalog_summary(client.import_openapi(project_id, **kw)) for kw in openapi]
    out += [_catalog_summary(client.import_mcp(project_id, **kw)) for kw in mcp]
    for c in out:
        logging.info(
            "tool catalog %s %s r%s %s (%d tools)",
            c["source"],
            c["name"],
            c["revision"],
            "imported" if c["created"] else "unchanged",
            c["tools"],
        )
    return out


def _read_imports(directory: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The import requests of ``imports.yaml``, read before any is sent
    (a malformed file imports nothing). Raises ValueError or OSError."""
    path = directory / "imports.yaml"
    if not path.exists():
        return [], []
    try:
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        openapi = [
            {
                "name": str(api["name"]),
                "document": (directory / str(api["document"])).read_text(encoding="utf-8"),
                "service": api.get("service"),
                "risk_overrides": api.get("risk_overrides"),
                "names": api.get("names"),
            }
            for api in spec.get("openapi") or ()
        ]
        mcp = []
        for server in spec.get("mcp") or ():
            listed = json.loads((directory / str(server["tools"])).read_text(encoding="utf-8"))
            mcp.append(
                {
                    "server": dict(server["server"]),
                    "tools": list(listed["tools"]),
                    "protocol_version": server.get("protocol_version"),
                    "risk_overrides": server.get("risk_overrides"),
                    "names": server.get("names"),
                    "trust_annotations": bool(server.get("trust_annotations", False)),
                }
            )
    except (yaml.YAMLError, json.JSONDecodeError, AttributeError, KeyError, TypeError) as err:
        raise ValueError(f"{path} is malformed: {err!r}") from None
    return openapi, mcp


def _catalog_summary(res: Mapping[str, Any]) -> dict[str, Any]:
    counts = res.get("summary") or {}
    return {
        "source": res.get("source"),
        "name": res.get("name"),
        "revision": res.get("revision"),
        "created": bool(res.get("created")),
        "tools": counts.get("tools", 0),
        "kept": counts.get("kept", 0),
        "warnings": list(res.get("warnings") or ()),
    }


def _parse_pairs(spec: str, option: str) -> list[tuple[str, str]]:
    """``BASE:CANDIDATE[,BASE:CANDIDATE...]`` (repeats dropped)."""
    out: list[tuple[str, str]] = []
    for part in _parse_list(spec):
        pair = _parse_pair(part, option)
        if pair is not None and pair not in out:
            out.append(pair)
    return out


def _impact_summary(change_set: Mapping[str, Any], impact: Mapping[str, Any]) -> dict[str, Any]:
    graph = impact.get("graph") or {}
    return {
        "change_set_id": change_set.get("id"),
        "base": (change_set.get("base") or {}).get("version"),
        "candidate": (change_set.get("candidate") or {}).get("version"),
        "created": bool(change_set.get("created")),
        "changes": [_change_line(i) for i in change_set.get("items") or ()],
        "complete": bool(impact.get("complete")),
        "problems": [f"{p.get('service')}: {p.get('code')}" for p in impact.get("problems") or ()],
        "unresolved": [
            f"{(s.get('component') or {}).get('kind')}:{(s.get('component') or {}).get('key')}"
            for s in graph.get("unresolved") or ()
        ],
        # Each selected scenario with why (the acceptance of spec Phase 4).
        "scenarios": {str(sc.get("name")): list(sc.get("why") or ()) for sc in impact.get("scenarios") or ()},
        "irreversible_actions": sorted(str(a.get("label")) for a in impact.get("irreversible_actions") or ()),
        "new_privileges": [
            f"{p.get('tool')} ({p.get('change')})" for p in impact.get("new_privileges") or ()
        ],
    }


def _change_line(item: Mapping[str, Any]) -> str:
    """A change item as one line: what changed, then how (a prompt's
    subject is its hash, which says nothing)."""
    summary, subject = str(item.get("summary") or ""), str(item.get("subject") or "")
    if item.get("kind") == "prompt" or subject in summary:
        return summary
    return f"{item.get('kind')} {subject}: {summary}"


def _impact_settled(summary: Mapping[str, Any]) -> bool:
    """Both services answered in full, and the graph knows every changed
    component (it ingests registrations asynchronously)."""
    return bool(summary.get("complete")) and not summary.get("unresolved")


def _impacts(
    client: Client, change_sets: Sequence[Mapping[str, Any]], *, timeout_s: float, interval_s: float = 2.0
) -> list[dict[str, Any]]:
    """The impact of each change set, asked again until it has settled or
    ``timeout_s`` passed: a component registered a moment ago may not be in
    the dependency graph yet."""
    deadline = time.monotonic() + timeout_s
    out: list[dict[str, Any]] = []
    for cs in change_sets:
        while True:
            summary = _impact_summary(cs, client.change_set_impact(str(cs["id"])))
            if _impact_settled(summary) or time.monotonic() >= deadline:
                break
            time.sleep(interval_s)
        logging.info(
            "change set %s -> %s: %d scenarios required%s",
            summary["base"],
            summary["candidate"],
            len(summary["scenarios"]),
            "" if _impact_settled(summary) else " (incomplete)",
        )
        out.append(summary)
    return out


def _settle_impact(client: Client, change_set_id: str, *, deadline: float, interval_s: float = 2.0) -> bool:
    """Waits until the dependency graph knows every component a change set
    changed (or ``deadline`` passes): a release evaluated before that pins
    a suite from an impact that misses the change."""
    while True:
        impact = client.change_set_impact(change_set_id)
        unresolved = ((impact.get("graph") or {}).get("unresolved")) or ()
        if impact.get("complete") and not unresolved:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval_s)


def _release_summary(release: Mapping[str, Any], gate: Mapping[str, Any], *, created: bool) -> dict[str, Any]:
    decision = gate.get("decision") or {}
    return {
        "release_id": release.get("id"),
        "baseline": (release.get("baseline") or {}).get("version"),
        "candidate": (release.get("candidate") or {}).get("version"),
        "created": created,
        "revision": gate.get("revision"),
        "status": gate.get("status"),
        "outcome": decision.get("outcome"),
        "effective_outcome": gate.get("effective_outcome"),
        "exit_code": gate.get("exit_code"),
        "incomplete": decision.get("incomplete"),
        "rules": [str(r.get("rule")) for r in decision.get("rules") or ()],
        "risk_index": (decision.get("risk_index") or {}).get("value"),
        "evidence_sha256": gate.get("evidence_sha256"),
        "evidence_verified": gate.get("evidence_verified"),
    }


def _release_healthy(summary: Mapping[str, Any]) -> bool:
    """A gate the demo can show: it decided on complete evidence that still
    verifies. BLOCK is expected (1.3.0 is the bad candidate)."""
    return (
        summary.get("status") == "DECIDED"
        and summary.get("incomplete") is False
        and summary.get("evidence_verified") is True
    )


def _gate_releases(
    client: Client,
    project_id: str,
    agent: str,
    pairs: Sequence[tuple[str, str]],
    *,
    impact_timeout_s: float,
    gate_timeout_s: float,
) -> list[dict[str, Any]]:
    """A release of each pair, gated. A release of the pair from an earlier
    seed is kept (its gate is not recomputed: decisions are immutable); a new
    one is evaluated once the dependency graph knows its change."""
    existing = client.releases(project_id, agent=agent)
    out: list[dict[str, Any]] = []
    for base, candidate in pairs:
        known = [
            r
            for r in existing
            if (r.get("baseline") or {}).get("version") == base
            and (r.get("candidate") or {}).get("version") == candidate
        ]
        if known:
            release, created = known[0], False
        else:
            release = client.create_release(
                project_id,
                agent,
                base,
                candidate,
                title=f"{base} -> {candidate} (demo)",
                evaluate=False,
                idempotency_key=f"seed-release-{base}-{candidate}",
            )["release"]
            created = True
        release_id = str(release["id"])
        if release.get("gate") is None:
            if not _settle_impact(
                client, str(release["change_set_id"]), deadline=time.monotonic() + impact_timeout_s
            ):
                logging.error(
                    "release %s -> %s: the dependency graph does not know the change yet", base, candidate
                )
            client.evaluate_release(release_id, idempotency_key=f"seed-evaluate-{release_id}")
            logging.info("release %s -> %s evaluating: %s", base, candidate, release_id)
        try:
            gate = client.wait_for_gate(release_id, timeout_s=gate_timeout_s)
        except APIError as err:
            logging.error("release %s -> %s: %s", base, candidate, err)
            out.append(
                {"release_id": release_id, "baseline": base, "candidate": candidate, "created": created}
                | {"status": None, "error": str(err)}
            )
            continue
        summary = _release_summary(release, gate, created=created)
        logging.info(
            "release %s -> %s: %s (exit code %s)",
            base,
            candidate,
            summary["effective_outcome"],
            summary["exit_code"],
        )
        out.append(summary)
    return out


def _evaluation_summary(detail: Mapping[str, Any]) -> dict[str, Any]:
    run = detail.get("run") or {}
    summary = detail.get("summary") or {}
    return {
        "run_id": run.get("id"),
        "baseline": run.get("baseline_version"),
        "candidate": run.get("candidate_version"),
        "status": run.get("status"),
        "error": run.get("error"),
        "counts": run.get("counts"),
        "new_critical_failures": sorted(
            str(n.get("scenario_name")) for n in summary.get("new_critical_failures") or ()
        ),
        "regressed": sorted(str(n) for n in summary.get("regressed") or ()),
        "incomplete": sorted(str(n) for n in summary.get("incomplete") or ()),
    }


def _evaluation_healthy(evaluation: Mapping[str, Any]) -> bool:
    """A comparison the demo can show: every case was compared. Regressions
    are expected (the eager candidate has them); an incomplete case means a
    side could not run."""
    return evaluation.get("status") == "COMPLETED" and not evaluation.get("incomplete")


def _parse_pair(spec: str, option: str = "--evaluate") -> tuple[str, str] | None:
    """``BASELINE:CANDIDATE`` (``""`` for none)."""
    if not spec.strip():
        return None
    baseline, sep, candidate = spec.partition(":")
    if not sep or not baseline.strip() or not candidate.strip() or baseline.strip() == candidate.strip():
        raise ValueError(f"{option} expects BASELINE:CANDIDATE (two different versions), got {spec!r}")
    return baseline.strip(), candidate.strip()


def _simulation_summary(version: str, detail: Mapping[str, Any]) -> dict[str, Any]:
    run = detail.get("run") or {}
    cases = detail.get("cases") or []
    return {
        "version": version,
        "run_id": run.get("id"),
        "status": run.get("status"),
        "cases": run.get("case_count"),
        **{k: run.get(k) for k in ("passed", "failed", "errored", "critical_failures")},
        "failed_scenarios": sorted(str(c.get("scenario_name")) for c in cases if c.get("status") == "FAILED"),
        "errored_scenarios": sorted(
            str(c.get("scenario_name")) for c in cases if c.get("status") == "ERRORED"
        ),
    }


def _healthy(simulation: Mapping[str, Any]) -> bool:
    """A run the demo can show: it finished and every case was evaluated.
    Failed cases are expected (the eager candidate has regressions); errored
    ones mean the stack is misconfigured (the agent could not be reached)."""
    return simulation.get("status") == "COMPLETED" and not simulation.get("errored")


def cmd_seed(args: argparse.Namespace) -> int:
    """Loads the demo workspace. Idempotent for manifests, the tool twin and
    the scenarios; every run adds another batch of production traffic and
    another set of simulation runs."""
    cfg = Config.from_env(service_name="support-refund-agent")
    store = ManifestStore()
    simulate = _parse_list(args.simulate)
    unknown = [v for v in simulate if v not in store.versions]
    if unknown:
        logging.error("--simulate: unknown agent version(s) %s; known: %s", unknown, store.versions)
        return 2
    try:
        pair = _parse_pair(args.evaluate)
        changes = _parse_pairs(args.changes, "--changes")
        release_pairs = _parse_pairs(args.releases, "--releases")
    except ValueError as err:
        logging.error("%s", err)
        return 2
    if pair is not None and any(v not in store.versions for v in pair):
        logging.error("--evaluate: unknown agent version(s) in %s; known: %s", pair, store.versions)
        return 2
    unknown_pairs = [c for c in changes if any(v not in store.versions for v in c)]
    if unknown_pairs:
        logging.error("--changes: unknown agent version(s) in %s; known: %s", unknown_pairs, store.versions)
        return 2
    unknown_releases = [c for c in release_pairs if any(v not in store.versions for v in c)]
    if unknown_releases:
        logging.error(
            "--releases: unknown agent version(s) in %s; known: %s", unknown_releases, store.versions
        )
        return 2
    started: list[tuple[str, str]] = []
    change_sets: list[dict[str, Any]] = []
    suite: dict[str, Any] | None = None
    evaluation_id: str | None = None
    try:
        client = Client.from_config(cfg, timeout_s=30)
        client.wait_ready(timeout_s=args.wait)
        project_id = client.project_id(args.project)
        registered = []
        for version in store.versions:
            path = store.path(version)
            res = client.register_manifest(project_id, path.read_text(encoding="utf-8"))
            registered.append({"version": version, "created": bool(res.get("created"))})
            logging.info(
                "agent manifest %s@%s %s",
                store.get(version).name,
                version,
                "registered" if res.get("created") else "already registered",
            )
        catalogs = _import_catalogs(client, project_id, args.assurance_dir)
        assurance = _register_assurance(client, project_id, args.assurance_dir)
        # Change sets are computed now; their impact is asked at the end,
        # once the dependency graph has taken in what was just registered.
        for base, candidate in changes:
            cs = client.create_change_set(
                project_id, store.get(candidate).name, base, candidate, title=f"{base} -> {candidate} (demo)"
            )
            change_sets.append(cs)
            logging.info(
                "change set %s -> %s %s: %s",
                base,
                candidate,
                "computed" if cs.get("created") else "unchanged",
                cs.get("id"),
            )
        # Started before the traffic so that the worker runs them meanwhile.
        for version in simulate:
            res = client.start_simulation(project_id, store.get(version).name, version, seed=args.seed)
            run_id = str(res["run"]["id"])
            started.append((version, run_id))
            logging.info(
                "simulation of %s queued: %s (%d cases)", version, run_id, len(res.get("cases") or [])
            )
        if pair is not None:
            names = [*assurance["scenarios"]["saved"], *assurance["scenarios"]["unchanged"]]
            suite = _ensure_suite(client, project_id, names)
            baseline, candidate = pair
            res = client.start_eval_run(
                project_id,
                store.get(candidate).name,
                baseline,
                candidate,
                dataset_id=str(suite["id"]),
                seed=args.seed,
            )
            evaluation_id = str(res["run"]["id"])
            logging.info("evaluation of %s against %s queued: %s", candidate, baseline, evaluation_id)
    except APIError as err:
        logging.error("seed failed: %s", err)
        return 1
    except (OSError, ValueError) as err:
        logging.error("seed failed: cannot read the assurance assets: %s", err)
        return 1

    records: list[dict[str, Any]] = []
    if args.count > 0:
        records = _traffic(args, cfg, verify_outcomes=True)

    simulations: list[dict[str, Any]] = []
    for version, run_id in started:
        try:
            detail = client.wait_for_simulation(run_id, timeout_s=args.simulation_timeout)
        except APIError as err:
            logging.error("simulation %s of %s: %s", run_id, version, err)
            simulations.append({"version": version, "run_id": run_id, "status": None, "error": str(err)})
            continue
        simulations.append(_simulation_summary(version, detail))
    for sim in simulations:
        if not _healthy(sim):
            logging.error(
                "simulation %s of %s is not healthy: status %s, errored %s",
                sim["run_id"],
                sim["version"],
                sim.get("status"),
                sim.get("errored_scenarios") or sim.get("error"),
            )

    evaluation: dict[str, Any] | None = None
    if evaluation_id is not None:
        try:
            detail = client.wait_for_eval_run(evaluation_id, timeout_s=args.evaluation_timeout)
            evaluation = _evaluation_summary(detail)
        except APIError as err:
            logging.error("evaluation %s: %s", evaluation_id, err)
            evaluation = {"run_id": evaluation_id, "status": None, "error": str(err)}
        if not _evaluation_healthy(evaluation):
            logging.error(
                "evaluation %s is not healthy: status %s, incomplete %s",
                evaluation_id,
                evaluation.get("status"),
                evaluation.get("incomplete") or evaluation.get("error"),
            )

    impacts: list[dict[str, Any]] = []
    try:
        impacts = _impacts(client, change_sets, timeout_s=args.impact_timeout)
    except APIError as err:
        logging.error("change impact: %s", err)
        impacts = [
            {"change_set_id": cs.get("id"), "complete": False, "error": str(err)} for cs in change_sets
        ]
    for impact in impacts:
        if not _impact_settled(impact):
            logging.error(
                "the impact of change set %s is incomplete: problems %s, unresolved %s",
                impact.get("change_set_id"),
                impact.get("problems") or impact.get("error"),
                impact.get("unresolved"),
            )

    # Releases last: their evaluation pins a suite from the change's impact,
    # which needs the dependency graph to know what was just registered.
    releases: list[dict[str, Any]] = []
    if release_pairs:
        try:
            releases = _gate_releases(
                client,
                project_id,
                store.get(release_pairs[0][1]).name,
                release_pairs,
                impact_timeout_s=args.impact_timeout,
                gate_timeout_s=args.release_timeout,
            )
        except APIError as err:
            logging.error("releases: %s", err)
            releases = [
                {"baseline": b, "candidate": c, "status": None, "error": str(err)} for b, c in release_pairs
            ]
    for rel in releases:
        if not _release_healthy(rel):
            logging.error(
                "the gate of release %s -> %s is not usable: status %s, incomplete %s, verified %s",
                rel.get("baseline"),
                rel.get("candidate"),
                rel.get("status"),
                rel.get("incomplete"),
                rel.get("evidence_verified") if rel.get("status") else rel.get("error"),
            )

    verified = [r["verified_outcome"] for r in records if r.get("verified_outcome")]
    summary = {
        "project_id": project_id,
        "manifests": registered,
        "catalogs": catalogs,
        **assurance,
        "change_sets": impacts,
        "releases": releases,
        "simulations": simulations,
        "dataset": suite,
        "evaluation": evaluation,
        "conversations": len(records),
        "verified_outcomes": len(verified),
        "verified_outcomes_reported": sum(1 for v in verified if v.get("reported")),
        "claimed_success_contradicted": sum(
            1
            for r in records
            if r.get("verified_outcome", {}).get("status") == "FAILURE"
            and r.get("claimed_outcome") == "SUCCESS"
        ),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    ui = os.environ.get("AGENTTWIN_UI_URL")
    if ui:
        print(f"\nOpen {ui.rstrip('/')}/traces to explore the demo traces.")
    ok = (
        len(records) == args.count
        and all(_healthy(s) for s in simulations)
        and (evaluation is None or _evaluation_healthy(evaluation))
        and all(_impact_settled(i) for i in impacts)
        and all(_release_healthy(r) for r in releases)
    )
    return 0 if ok else 1


def _parse_list(spec: str | None) -> list[str]:
    """``a, b,,a`` -> ``[a, b]`` (order kept, empties and repeats dropped)."""
    out: list[str] = []
    for part in (spec or "").split(","):
        item = part.strip()
        if item and item not in out:
            out.append(item)
    return out


def _parse_versions(spec: str) -> dict[str, float]:
    versions: dict[str, float] = {}
    for part in spec.split(","):
        name, _, weight = part.partition("=")
        versions[name.strip()] = float(weight or 1)
    return versions


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    p = argparse.ArgumentParser(
        prog="support-refund-agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    tools_url = os.environ.get("DEMO_TOOLS_URL", DEFAULT_TOOLS_URL)

    s = sub.add_parser("serve-tools", help="serve the Demo Co tools")
    s.add_argument("--host", default="0.0.0.0")  # noqa: S104 - container entrypoint
    s.add_argument("--port", type=int, default=8091)
    s.add_argument("--faults", help="tool:kind:probability[,...]")
    s.add_argument("--seed", type=int, default=7)
    s.set_defaults(fn=cmd_serve_tools)

    s = sub.add_parser("serve-agent", help="serve the agent adapter API")
    s.add_argument("--host", default="0.0.0.0")  # noqa: S104 - container entrypoint
    s.add_argument("--port", type=int, default=8090)
    s.add_argument("--tools-url", default=tools_url)
    s.set_defaults(fn=cmd_serve_agent)

    s = sub.add_parser("run", help="run one conversation")
    s.add_argument("text")
    s.add_argument("--customer", default="CUS-100")
    s.add_argument("--version")
    s.add_argument("--tools-url", default=tools_url)
    s.add_argument("--agent-url", default=os.environ.get("DEMO_AGENT_URL"))
    s.add_argument(
        "--new-order",
        type=float,
        metavar="TOTAL",
        help="create a fresh delivered order; TEXT may use {order}",
    )
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("traffic", help="generate production-like traffic")
    s.add_argument("--count", type=int, default=20)
    s.add_argument("--versions", default="1.2.4=0.8,1.3.0=0.2")
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--tools-url", default=tools_url)
    s.add_argument("--agent-url", default=os.environ.get("DEMO_AGENT_URL"))
    s.add_argument("--verify-outcomes", action="store_true")
    s.add_argument("--delay", type=float, default=0.0)
    s.set_defaults(fn=cmd_traffic)

    s = sub.add_parser("seed", help="load the demo workspace (agent, twin, scenarios, runs, traffic)")
    s.add_argument("--project", default=os.environ.get("AGENTTWIN_PROJECT", "support"))
    s.add_argument("--count", type=int, default=40)
    s.add_argument("--versions", default="1.2.4=0.8,1.3.0=0.2")
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--tools-url", default=tools_url)
    s.add_argument("--agent-url", default=os.environ.get("DEMO_AGENT_URL"))
    s.add_argument("--wait", type=float, default=180.0, help="seconds to wait for the control plane")
    s.add_argument(
        "--assurance-dir",
        type=Path,
        default=default_assurance_dir(),
        help="the tool twin (twin.yaml), scenarios/*.yaml and the tool catalogs (imports.yaml)",
    )
    s.add_argument(
        "--simulate",
        default="",
        metavar="VERSIONS",
        help="comma-separated agent versions to simulate against every scenario",
    )
    s.add_argument(
        "--simulation-timeout",
        type=float,
        default=600.0,
        help="seconds to wait for each simulation run to finish",
    )
    s.add_argument(
        "--evaluate",
        default="",
        metavar="BASELINE:CANDIDATE",
        help=f"evaluate CANDIDATE against BASELINE on the {SUITE} dataset (kept by the seed)",
    )
    s.add_argument(
        "--evaluation-timeout",
        type=float,
        default=900.0,
        help="seconds to wait for the evaluation run to finish",
    )
    s.add_argument(
        "--changes",
        default="",
        metavar="BASE:CANDIDATE[,...]",
        help="compare versions: a change set each, with the scenarios the change requires and why",
    )
    s.add_argument(
        "--releases",
        default="",
        metavar="BASELINE:CANDIDATE[,...]",
        help="gate a release of each CANDIDATE against its BASELINE (an earlier seed's release is kept)",
    )
    s.add_argument(
        "--release-timeout",
        type=float,
        default=900.0,
        help="seconds to wait for each release's gate to decide",
    )
    s.add_argument(
        "--impact-timeout",
        type=float,
        default=60.0,
        help="seconds to wait for the dependency graph to know every changed component",
    )
    s.set_defaults(fn=cmd_seed)

    args = p.parse_args(argv)
    code: int = args.fn(args)
    return code


if __name__ == "__main__":
    sys.exit(main())
