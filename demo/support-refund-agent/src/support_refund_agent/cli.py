"""Command line entry points for the demo.

support-refund-agent serve-tools   Demo Co tools over HTTP (production tools)
support-refund-agent serve-agent   the agent adapter HTTP server
support-refund-agent run TEXT      one conversation; prints the result and trace id
support-refund-agent traffic       production-like traffic (optionally verified)
support-refund-agent seed          load the demo workspace: register the agent
                                   manifests, the tool twin and the scenarios, run
                                   simulations and send verified production traffic
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from agenttwin import AgentTwin, APIError, Client, Config
from support_refund_agent.agent import Agent, ManifestStore, RunRequest, scripted_model_factory
from support_refund_agent.server import AgentServer
from support_refund_agent.tools_server import ToolsServer, parse_faults
from support_refund_agent.traffic import TrafficGenerator
from support_refund_agent.world import INTERNAL_API_KEY

DEFAULT_TOOLS_URL = "http://127.0.0.1:8091"


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
    started: list[tuple[str, str]] = []
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
        assurance = _register_assurance(client, project_id, args.assurance_dir)
        # Started before the traffic so that the worker runs them meanwhile.
        for version in simulate:
            res = client.start_simulation(project_id, store.get(version).name, version, seed=args.seed)
            run_id = str(res["run"]["id"])
            started.append((version, run_id))
            logging.info(
                "simulation of %s queued: %s (%d cases)", version, run_id, len(res.get("cases") or [])
            )
    except APIError as err:
        logging.error("seed failed: %s", err)
        return 1
    except OSError as err:
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

    verified = [r["verified_outcome"] for r in records if r.get("verified_outcome")]
    summary = {
        "project_id": project_id,
        "manifests": registered,
        **assurance,
        "simulations": simulations,
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
    ok = len(records) == args.count and all(_healthy(s) for s in simulations)
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
        help="the tool twin (twin.yaml) and scenarios/*.yaml to register",
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
    s.set_defaults(fn=cmd_seed)

    args = p.parse_args(argv)
    code: int = args.fn(args)
    return code


if __name__ == "__main__":
    sys.exit(main())
