"""A small client for the AgentTwin REST API (standard library only).

Authenticates with a project API key (``X-AgentTwin-Api-Key``). Covers what
an agent codebase or its CI needs: finding the project, registering agent
manifests, tool twins and scenarios, running simulations, keeping datasets,
evaluating a candidate version against its baseline, gating a release and
recording outcomes.
Errors carry the API's machine-readable code (``APIError.code``) and never
include the key.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agenttwin.config import Config

__all__ = ["APIError", "Client"]

_MAX_RESPONSE_BYTES = 32 << 20
_FINAL_RUN_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})


@dataclass
class APIError(Exception):
    """The API rejected the request or could not be reached (``status`` 0)."""

    status: int
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.status} {self.code}: {self.message}"


class Client:
    """Thin REST client. ``api_url`` is the control-plane base URL, e.g.
    ``https://agenttwin.example.com`` (``/api/v1`` is added per call)."""

    def __init__(self, api_url: str, api_key: str, *, timeout_s: float = 10.0) -> None:
        parsed = urllib.parse.urlsplit(api_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("api_url must be an http(s) URL")
        if not api_key:
            raise ValueError("api_key is required")
        self.api_url = api_url.rstrip("/")
        self._api_key = api_key
        self.timeout_s = timeout_s

    @classmethod
    def from_config(cls, config: Config | None = None, *, timeout_s: float = 10.0) -> Client:
        cfg = config or Config.from_env()
        if not cfg.api_url or not cfg.api_key:
            raise ValueError(
                "the API client needs api_url and api_key (AGENTTWIN_API_URL / AGENTTWIN_API_KEY)"
            )
        return cls(cfg.api_url, cfg.api_key, timeout_s=timeout_s)

    def __repr__(self) -> str:  # never print the key
        return f"Client(api_url={self.api_url!r})"

    # ------------------------------------------------------------------ core
    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        data: bytes | None = None,
        content_type: str | None = None,
        query: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Performs one call and returns the decoded JSON body (``None`` for
        empty bodies). Raises APIError for non-2xx responses."""
        if not path.startswith("/"):
            raise ValueError("path must start with /")
        url = self.api_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        hdrs = {"Accept": "application/json", "X-AgentTwin-Api-Key": self._api_key}
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            content_type = "application/json"
        if content_type:
            hdrs["Content-Type"] = content_type
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, method=method.upper(), headers=hdrs)  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310 - scheme validated
                raw = resp.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as err:
            raise _api_error(err) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
            reason = getattr(err, "reason", err)
            raise APIError(0, "UNAVAILABLE", f"{self.api_url} is not reachable: {reason}") from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise APIError(0, "RESPONSE_TOO_LARGE", "response exceeded the client limit")
        if not raw:
            return None
        return json.loads(raw)

    def wait_ready(self, timeout_s: float = 120.0, interval_s: float = 1.0) -> None:
        """Blocks until the control plane reports ready (``/health/ready``)."""
        deadline = time.monotonic() + timeout_s
        last = "no response"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(self.api_url + "/health/ready", timeout=3) as resp:  # noqa: S310
                    if resp.status == 200:
                        return
                    last = f"HTTP {resp.status}"
            except urllib.error.HTTPError as err:
                last = f"HTTP {err.code}"
            except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
                last = str(getattr(err, "reason", err))
            time.sleep(interval_s)
        raise APIError(
            0, "NOT_READY", f"{self.api_url} did not become ready within {timeout_s:.0f}s ({last})"
        )

    # ------------------------------------------------------------ resources
    def projects(self) -> list[dict[str, Any]]:
        body = self.request("GET", "/api/v1/projects")
        items: list[dict[str, Any]] = body.get("items", []) if isinstance(body, dict) else []
        return items

    def project_id(self, slug_or_id: str) -> str:
        """Resolves a project slug (or id) visible to this key."""
        for p in self.projects():
            if slug_or_id in (p.get("slug"), p.get("id")):
                return str(p["id"])
        raise APIError(404, "PROJECT_NOT_FOUND", f"no project {slug_or_id!r} is visible to this API key")

    def register_manifest(
        self,
        project_id: str,
        manifest: str,
        *,
        commit_sha: str | None = None,
        repo_url: str | None = None,
        branch: str | None = None,
    ) -> dict[str, Any]:
        """Registers an agent manifest (YAML or JSON text). Idempotent for
        identical content; a changed manifest under an existing version is
        rejected with ``VERSION_EXISTS`` (versions are immutable)."""
        pairs = (("commit_sha", commit_sha), ("repo_url", repo_url), ("branch", branch))
        query = {k: v for k, v in pairs if v}
        result: dict[str, Any] = self.request(
            "POST",
            f"/api/v1/projects/{urllib.parse.quote(project_id, safe='')}/agent-manifests",
            data=manifest.encode("utf-8"),
            content_type="application/yaml",
            query=query or None,
        )
        return result

    # ---------------------------------------------- tool catalogs, changes
    def import_openapi(
        self,
        project_id: str,
        name: str,
        document: str | Mapping[str, Any],
        *,
        service: str | None = None,
        risk_overrides: Mapping[str, str] | None = None,
        names: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Imports the operations of an OpenAPI document (YAML or JSON text,
        or the parsed object) as the tool catalog ``name``. A tool an agent
        manifest declares keeps the manifest's definition; the graph still
        links it to the API (and the API to ``service``). An import equal to
        the latest revision stores nothing (``created`` is false)."""
        body: dict[str, Any] = {
            "name": name,
            "document": document if isinstance(document, str) else dict(document),
        }
        optional = (
            ("service", service),
            ("risk_overrides", dict(risk_overrides) if risk_overrides is not None else None),
            ("names", dict(names) if names is not None else None),
        )
        body |= {k: v for k, v in optional if v is not None}
        result: dict[str, Any] = self.request(
            "POST",
            f"/api/v1/projects/{urllib.parse.quote(project_id, safe='')}/imports/openapi",
            json_body=body,
        )
        return result

    def import_mcp(
        self,
        project_id: str,
        server: Mapping[str, str],
        tools: Sequence[Mapping[str, Any]],
        *,
        protocol_version: str | None = None,
        risk_overrides: Mapping[str, str] | None = None,
        names: Mapping[str, str] | None = None,
        trust_annotations: bool = False,
    ) -> dict[str, Any]:
        """Imports the tools of an MCP server's ``tools/list`` result (every
        page, in order). The server is not contacted. Its annotations set
        risk only with ``trust_annotations``; otherwise a tool is
        ``WRITE_IRREVERSIBLE`` unless ``risk_overrides`` says otherwise."""
        body: dict[str, Any] = {"server": dict(server), "tools": [dict(t) for t in tools]}
        optional = (
            ("protocol_version", protocol_version),
            ("risk_overrides", dict(risk_overrides) if risk_overrides is not None else None),
            ("names", dict(names) if names is not None else None),
            ("trust_annotations", True if trust_annotations else None),
        )
        body |= {k: v for k, v in optional if v is not None}
        result: dict[str, Any] = self.request(
            "POST", f"/api/v1/projects/{urllib.parse.quote(project_id, safe='')}/imports/mcp", json_body=body
        )
        return result

    def create_change_set(
        self,
        project_id: str,
        agent: str,
        base_version: str,
        candidate_version: str,
        *,
        title: str | None = None,
        git: Mapping[str, Any] | None = None,
        declared: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Compares two registered versions of an agent and stores what
        changed. The same request answers the stored change set (``created``
        is false), so a re-run CI job does not store a copy."""
        body: dict[str, Any] = {
            "agent": agent,
            "base_version": base_version,
            "candidate_version": candidate_version,
        }
        optional = (
            ("title", title),
            ("git", dict(git) if git is not None else None),
            ("declared", [dict(d) for d in declared] if declared is not None else None),
        )
        body |= {k: v for k, v in optional if v is not None}
        result: dict[str, Any] = self.request(
            "POST", f"/api/v1/projects/{urllib.parse.quote(project_id, safe='')}/change-sets", json_body=body
        )
        return result

    def change_set_impact(self, change_set_id: str) -> dict[str, Any]:
        """The scenarios a change set requires, each with why, and what the
        change reaches. ``complete`` is false when a service could not
        answer: the impact then says what is missing (``problems``)."""
        result: dict[str, Any] = self.request(
            "GET", f"/api/v1/change-sets/{urllib.parse.quote(change_set_id, safe='')}/impact"
        )
        return result

    def register_twin(self, project_id: str, twin: str) -> dict[str, Any]:
        """Registers a tool-twin definition (YAML or JSON text). Identical
        content is a no-op (``created`` is false); changed content becomes a
        new version of the twin."""
        result: dict[str, Any] = self.request(
            "POST", "/api/v1/twins", json_body={"project_id": project_id, "yaml": twin}
        )
        return result

    def validate_scenario(self, project_id: str, scenario: str) -> dict[str, Any]:
        """Checks a scenario (YAML or JSON text) against its schema and the
        twin it names without saving it: ``{valid, problems, warnings, ...}``."""
        result: dict[str, Any] = self.request(
            "POST", "/api/v1/scenarios/validate", json_body={"project_id": project_id, "yaml": scenario}
        )
        return result

    def save_scenario(self, project_id: str, scenario: str) -> dict[str, Any]:
        """Creates a scenario or a new version of it; identical content is a
        no-op. An invalid scenario raises ``SCENARIO_INVALID`` with the
        problems in ``APIError.details``."""
        result: dict[str, Any] = self.request(
            "POST", "/api/v1/scenarios", json_body={"project_id": project_id, "yaml": scenario}
        )
        return result

    def start_simulation(
        self,
        project_id: str,
        agent: str,
        agent_version: str,
        *,
        scenarios: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
        seed: int | None = None,
        release_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Queues a simulation run of an agent version against its scenarios
        (all of them unless ``scenarios`` or ``tags`` narrow the selection).
        With ``idempotency_key`` (8-128 of ``[A-Za-z0-9._:-]``) a retried
        request returns the run the first attempt created instead of starting
        another one."""
        body: dict[str, Any] = {"project_id": project_id, "agent": agent, "agent_version": agent_version}
        if scenarios is not None:
            body["scenarios"] = list(scenarios)
        if tags is not None:
            body["tags"] = list(tags)
        if seed is not None:
            body["seed"] = seed
        if release_id is not None:
            body["release_id"] = release_id
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        result: dict[str, Any] = self.request("POST", "/api/v1/simulations", json_body=body, headers=headers)
        return result

    def simulation(self, run_id: str) -> dict[str, Any]:
        """A simulation run with its cases and state transitions."""
        result: dict[str, Any] = self.request(
            "GET", f"/api/v1/simulations/{urllib.parse.quote(run_id, safe='')}"
        )
        return result

    def wait_for_simulation(
        self, run_id: str, *, timeout_s: float = 600.0, interval_s: float = 2.0
    ) -> dict[str, Any]:
        """Polls until the run is COMPLETED, FAILED or CANCELLED."""
        deadline = time.monotonic() + timeout_s
        while True:
            detail = self.simulation(run_id)
            status = str((detail.get("run") or {}).get("status"))
            if status in _FINAL_RUN_STATUSES:
                return detail
            if time.monotonic() >= deadline:
                raise APIError(0, "TIMEOUT", f"simulation {run_id} is still {status} after {timeout_s:.0f}s")
            time.sleep(interval_s)

    # ------------------------------------------------------------ evaluation
    def datasets(self, project_id: str, *, name: str | None = None) -> list[dict[str, Any]]:
        """The project's datasets (not archived); with ``name``, only the one
        of that name (an empty list when there is none)."""
        query = {"project_id": project_id, "limit": "200"}
        if name is not None:
            query["q"] = name
        body = self.request("GET", "/api/v1/datasets", query=query)
        items: list[dict[str, Any]] = body.get("items", []) if isinstance(body, dict) else []
        return [d for d in items if name is None or d.get("name") == name]

    def dataset(self, dataset_id: str, *, version: int | None = None) -> dict[str, Any]:
        """A dataset with one version's cases (the latest unless ``version``)."""
        query = {"version": str(version)} if version is not None else None
        result: dict[str, Any] = self.request(
            "GET", f"/api/v1/datasets/{urllib.parse.quote(dataset_id, safe='')}", query=query
        )
        return result

    def create_dataset(
        self,
        project_id: str,
        name: str,
        cases: Sequence[str | Mapping[str, Any]],
        *,
        description: str | None = None,
        tags: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Creates a dataset whose first version holds ``cases`` (scenario
        names, or case objects). A name the project already uses raises
        ``DATASET_EXISTS``."""
        body: dict[str, Any] = {"project_id": project_id, "name": name, "cases": _cases(cases)}
        if description is not None:
            body["description"] = description
        if tags is not None:
            body["tags"] = list(tags)
        result: dict[str, Any] = self.request("POST", "/api/v1/datasets", json_body=body)
        return result

    def add_dataset_cases(
        self, dataset_id: str, cases: Sequence[str | Mapping[str, Any]], *, note: str | None = None
    ) -> dict[str, Any]:
        """Adds or updates cases: a new version when anything changed, the
        latest one otherwise."""
        body: dict[str, Any] = {"cases": _cases(cases)}
        if note is not None:
            body["note"] = note
        result: dict[str, Any] = self.request(
            "POST", f"/api/v1/datasets/{urllib.parse.quote(dataset_id, safe='')}/cases", json_body=body
        )
        return result

    def start_eval_run(
        self,
        project_id: str,
        agent: str,
        baseline_version: str,
        candidate_version: str,
        *,
        dataset_id: str | None = None,
        dataset_version: int | None = None,
        scenarios: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
        seed: int | None = None,
        release_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Queues an evaluation of a candidate version against its baseline
        on one suite: a dataset (its latest version unless
        ``dataset_version``), or scenarios and tags (every scenario when
        neither is given). With ``idempotency_key`` a retried request returns
        the run the first attempt created."""
        body: dict[str, Any] = {
            "project_id": project_id,
            "agent": agent,
            "baseline_version": baseline_version,
            "candidate_version": candidate_version,
        }
        optional = (
            ("dataset_id", dataset_id),
            ("dataset_version", dataset_version),
            ("scenarios", list(scenarios) if scenarios is not None else None),
            ("tags", list(tags) if tags is not None else None),
            ("seed", seed),
            ("release_id", release_id),
        )
        body |= {k: v for k, v in optional if v is not None}
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        result: dict[str, Any] = self.request("POST", "/api/v1/eval-runs", json_body=body, headers=headers)
        return result

    def eval_run(self, eval_run_id: str) -> dict[str, Any]:
        """An evaluation run with its summary and compared cases."""
        result: dict[str, Any] = self.request(
            "GET", f"/api/v1/eval-runs/{urllib.parse.quote(eval_run_id, safe='')}"
        )
        return result

    def wait_for_eval_run(
        self, eval_run_id: str, *, timeout_s: float = 900.0, interval_s: float = 2.0
    ) -> dict[str, Any]:
        """Polls until the evaluation run is COMPLETED, FAILED or CANCELLED."""
        deadline = time.monotonic() + timeout_s
        while True:
            detail = self.eval_run(eval_run_id)
            status = str((detail.get("run") or {}).get("status"))
            if status in _FINAL_RUN_STATUSES:
                return detail
            if time.monotonic() >= deadline:
                raise APIError(
                    0, "TIMEOUT", f"evaluation run {eval_run_id} is still {status} after {timeout_s:.0f}s"
                )
            time.sleep(interval_s)

    # -------------------------------------------------------------- releases
    def create_release(
        self,
        project_id: str,
        agent: str,
        baseline_version: str,
        candidate_version: str,
        *,
        title: str | None = None,
        git: Mapping[str, Any] | None = None,
        declared: Sequence[Mapping[str, Any]] | None = None,
        ci_url: str | None = None,
        evaluate: bool | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Creates a release of a candidate version against its baseline and,
        unless ``evaluate`` is false, starts its first evaluation:
        ``{release, gate}``. Every call creates a release; pass an
        ``idempotency_key`` so a retried request answers the first one."""
        body: dict[str, Any] = {
            "project_id": project_id,
            "agent": agent,
            "baseline_version": baseline_version,
            "candidate_version": candidate_version,
        }
        optional = (
            ("title", title),
            ("git", dict(git) if git is not None else None),
            ("declared", [dict(d) for d in declared] if declared is not None else None),
            ("ci_url", ci_url),
            ("evaluate", evaluate),
        )
        body |= {k: v for k, v in optional if v is not None}
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        result: dict[str, Any] = self.request("POST", "/api/v1/releases", json_body=body, headers=headers)
        return result

    def releases(
        self, project_id: str, *, agent: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """The project's releases, newest first (the first ``limit``), each
        with the gate of its latest evaluation."""
        query = {"project_id": project_id, "limit": str(limit)}
        if agent is not None:
            query["agent"] = agent
        body = self.request("GET", "/api/v1/releases", query=query)
        items: list[dict[str, Any]] = body.get("items", []) if isinstance(body, dict) else []
        return items

    def release(self, release_id: str) -> dict[str, Any]:
        """A release and the gate of each of its evaluations, newest first."""
        result: dict[str, Any] = self.request(
            "GET", f"/api/v1/releases/{urllib.parse.quote(release_id, safe='')}"
        )
        return result

    def evaluate_release(self, release_id: str, *, idempotency_key: str | None = None) -> dict[str, Any]:
        """Starts a new evaluation (revision) of a release; its gate."""
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        result: dict[str, Any] = self.request(
            "POST", f"/api/v1/releases/{urllib.parse.quote(release_id, safe='')}/evaluate", headers=headers
        )
        return result

    def release_gate(self, release_id: str, *, revision: int | None = None) -> dict[str, Any]:
        """The gate of the latest evaluation, or of ``revision``: its
        decision with the evidence, and ``exit_code`` for CI (0 pass,
        2 warn, 3 block; ``None`` while evaluating)."""
        query = {"revision": str(revision)} if revision is not None else None
        result: dict[str, Any] = self.request(
            "GET", f"/api/v1/releases/{urllib.parse.quote(release_id, safe='')}/gate", query=query
        )
        return result

    def wait_for_gate(
        self,
        release_id: str,
        *,
        revision: int | None = None,
        timeout_s: float = 1800.0,
        interval_s: float = 2.0,
    ) -> dict[str, Any]:
        """Polls until the gate decides (``status`` ``DECIDED``)."""
        deadline = time.monotonic() + timeout_s
        while True:
            gate = self.release_gate(release_id, revision=revision)
            if gate.get("status") == "DECIDED":
                return gate
            if time.monotonic() >= deadline:
                raise APIError(
                    0, "TIMEOUT", f"the gate of release {release_id} did not decide within {timeout_s:.0f}s"
                )
            time.sleep(interval_s)

    # ------------------------------------------------------------ regressions
    def regressions(
        self,
        project_id: str,
        *,
        status: Sequence[str] | None = None,
        severity: Sequence[str] | None = None,
        taxonomy: str | None = None,
        agent: str | None = None,
        include_merged: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """The project's regression inbox: production failures the miner
        grouped, most recently seen first (the first ``limit``)."""
        query = {"project_id": project_id, "limit": str(limit)}
        if status:
            query["status"] = ",".join(status)
        if severity:
            query["severity"] = ",".join(severity)
        if taxonomy is not None:
            query["taxonomy"] = taxonomy
        if agent is not None:
            query["agent"] = agent
        if include_merged:
            query["include_merged"] = "true"
        body = self.request("GET", "/api/v1/regressions/candidates", query=query)
        items: list[dict[str, Any]] = body.get("items", []) if isinstance(body, dict) else []
        return items

    def regression(self, regression_id: str) -> dict[str, Any]:
        """A regression with its failures and its history."""
        result: dict[str, Any] = self.request("GET", _regression_path(regression_id))
        return result

    def regression_draft(self, regression_id: str) -> dict[str, Any]:
        """The scenario its representative trace suggests (``draft.document``,
        ``draft.yaml``; ``draft.problems`` names what a person must write)."""
        result: dict[str, Any] = self.request("GET", _regression_path(regression_id, "draft"))
        return result

    def triage_regression(
        self,
        regression_id: str,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
        **changes: Any,
    ) -> dict[str, Any]:
        """Sets ``taxonomy``, ``severity``, ``tags`` or ``assignee``
        (``assignee=None`` unassigns)."""
        body = dict(changes) | ({"reason": reason} if reason is not None else {})
        result: dict[str, Any] = self.request(
            "PATCH", _regression_path(regression_id), json_body=body, headers=_idempotency(idempotency_key)
        )
        return result

    def confirm_regression(
        self, regression_id: str, *, reason: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self._regression_action(regression_id, "confirm", reason, idempotency_key)

    def dismiss_regression(
        self, regression_id: str, reason: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Not a failure worth a test; the reason is required."""
        return self._regression_action(regression_id, "dismiss", reason, idempotency_key)

    def reopen_regression(
        self, regression_id: str, reason: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """A dismissed or fixed regression is back; the reason is required."""
        return self._regression_action(regression_id, "reopen", reason, idempotency_key)

    def merge_regression(
        self,
        regression_id: str,
        into: str,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Moves the regression's failures into ``into`` (the same agent's)."""
        body: dict[str, Any] = {"into": into} | ({"reason": reason} if reason is not None else {})
        result: dict[str, Any] = self.request(
            "POST",
            _regression_path(regression_id, "merge"),
            json_body=body,
            headers=_idempotency(idempotency_key),
        )
        return result

    def promote_regression(
        self,
        regression_id: str,
        *,
        document: Mapping[str, Any] | None = None,
        yaml: str | None = None,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Makes the regression a regression test: its scenario (the draft
        as it is, or ``document`` / ``yaml``) joins the project's
        ``production-regressions`` dataset. Needs ``regression.promote``."""
        if document is not None and yaml is not None:
            raise ValueError("give the document or its YAML, not both")
        body: dict[str, Any] = {}
        if document is not None:
            body["document"] = dict(document)
        if yaml is not None:
            body["yaml"] = yaml
        if reason is not None:
            body["reason"] = reason
        result: dict[str, Any] = self.request(
            "POST",
            _regression_path(regression_id, "promote"),
            json_body=body,
            headers=_idempotency(idempotency_key),
        )
        return result

    def _regression_action(
        self, regression_id: str, action: str, reason: str | None, idempotency_key: str | None
    ) -> dict[str, Any]:
        body = {"reason": reason} if reason is not None else {}
        result: dict[str, Any] = self.request(
            "POST",
            _regression_path(regression_id, action),
            json_body=body,
            headers=_idempotency(idempotency_key),
        )
        return result

    # --------------------------------------------------- runtime containment
    def put_tool_endpoint(
        self,
        project_id: str,
        tool: str,
        *,
        url: str,
        risk: str,
        kind: str = "http",
        timeout_ms: int | None = None,
        idempotency: str | None = None,
        forward_headers: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Registers (or replaces) where the runtime gateway calls ``tool``
        (ADR-0033). The URL's host must be in the gateway's egress
        allowlist. Needs ``policy.write``."""
        body: dict[str, Any] = {"project_id": project_id, "url": url, "risk": risk, "kind": kind}
        if timeout_ms is not None:
            body["timeout_ms"] = timeout_ms
        if idempotency is not None:
            body["idempotency"] = idempotency
        if forward_headers is not None:
            body["forward_headers"] = list(forward_headers)
        result: dict[str, Any] = self.request("PUT", _tool_endpoint_path(tool), json_body=body)
        return result

    def tool_endpoints(self, project_id: str) -> list[dict[str, Any]]:
        body = self.request("GET", "/api/v1/tool-endpoints", query={"project_id": project_id})
        return _items(body)

    def policies(self, project_id: str, *, tool: str | None = None) -> list[dict[str, Any]]:
        """The project's runtime policies, optionally those guarding ``tool``."""
        query = {"project_id": project_id} | ({"tool": tool} if tool else {})
        return _items(self.request("GET", "/api/v1/policies", query=query))

    def policy(self, policy_id: str, project_id: str) -> dict[str, Any]:
        """A policy, its active version and every version."""
        result: dict[str, Any] = self.request(
            "GET", _policy_path(policy_id), query={"project_id": project_id}
        )
        return result

    def create_policy(
        self, project_id: str, document: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Stores a policy document (YAML or JSON) as version 1 of a new
        policy; nothing is activated. ``POLICY_EXISTS`` if its name is taken
        (:meth:`apply_policy` adds a version then)."""
        result: dict[str, Any] = self.request(
            "POST",
            "/api/v1/policies",
            json_body={"project_id": project_id, "document": document},
            headers=_idempotency(idempotency_key),
        )
        return result

    def add_policy_version(
        self, policy_id: str, project_id: str, document: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Stores the document as the policy's next version; a document that
        decides what the latest version decides returns it (``created``
        false)."""
        result: dict[str, Any] = self.request(
            "POST",
            _policy_path(policy_id, "versions"),
            json_body={"project_id": project_id, "document": document},
            headers=_idempotency(idempotency_key),
        )
        return result

    def apply_policy(self, project_id: str, document: str) -> dict[str, Any]:
        """Creates the policy, or adds the document as its next version when
        a policy of its name exists: ``{policy, version, created}``."""
        try:
            created = self.create_policy(project_id, document)
            return {"policy": created["policy"], "version": created["version"], "created": True}
        except APIError as err:
            if err.code != "POLICY_EXISTS":
                raise
        name = _policy_name(document)
        for p in self.policies(project_id):
            if p.get("name") == name:
                return self.add_policy_version(str(p["id"]), project_id, document)
        raise APIError(409, "POLICY_EXISTS", f"policy {name!r} exists but is not visible to this key")

    def test_policy(
        self,
        project_id: str,
        *,
        document: str | None = None,
        policy_id: str | None = None,
        version: int | None = None,
        cases: Sequence[Mapping[str, Any]] | None = None,
        base: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Decides a draft document (or a saved version) against its tests,
        the extra ``cases`` and around every numeric threshold. Stores
        nothing."""
        if (document is None) == (policy_id is None):
            raise ValueError("give a document or a policy_id, not both")
        body: dict[str, Any] = {"project_id": project_id}
        if document is not None:
            body["document"] = document
        if policy_id is not None:
            body["policy_id"] = policy_id
        if version is not None:
            body["version"] = version
        if cases:
            body["cases"] = [dict(c) for c in cases]
        if base is not None:
            body["base"] = dict(base)
        result: dict[str, Any] = self.request("POST", "/api/v1/policies/test", json_body=body)
        return result

    def activate_policy(
        self,
        policy_id: str,
        project_id: str,
        version: int,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Makes the version active from the next call on (only a tested
        version whose tests all pass). Needs ``policy.activate``."""
        body: dict[str, Any] = {"project_id": project_id, "version": version}
        if reason is not None:
            body["reason"] = reason
        result: dict[str, Any] = self.request(
            "POST", _policy_path(policy_id, "activate"), json_body=body, headers=_idempotency(idempotency_key)
        )
        return result

    def deactivate_policy(
        self, policy_id: str, project_id: str, reason: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        result: dict[str, Any] = self.request(
            "POST",
            _policy_path(policy_id, "deactivate"),
            json_body={"project_id": project_id, "reason": reason},
            headers=_idempotency(idempotency_key),
        )
        return result

    def approvals(
        self, project_id: str, *, status: str | None = None, tool: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Approval requests, newest first (the first ``limit``)."""
        query = {"project_id": project_id, "limit": str(limit)}
        if status is not None:
            query["status"] = status
        if tool is not None:
            query["tool"] = tool
        return _items(self.request("GET", "/api/v1/approvals", query=query))

    def approval(self, approval_id: str, project_id: str) -> dict[str, Any]:
        """The exact action waiting for a person, its expiry and history."""
        result: dict[str, Any] = self.request(
            "GET", _approval_path(approval_id), query={"project_id": project_id}
        )
        return result

    def approve(
        self, approval_id: str, project_id: str, reason: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Approves the exact action (once). Needs ``approval.decide``."""
        return self._decide(approval_id, "approve", project_id, reason, idempotency_key)

    def deny_approval(
        self, approval_id: str, project_id: str, reason: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self._decide(approval_id, "deny", project_id, reason, idempotency_key)

    def _decide(
        self, approval_id: str, action: str, project_id: str, reason: str, idempotency_key: str | None
    ) -> dict[str, Any]:
        result: dict[str, Any] = self.request(
            "POST",
            _approval_path(approval_id, action),
            json_body={"project_id": project_id, "reason": reason},
            headers=_idempotency(idempotency_key),
        )
        return result

    def policy_decisions(
        self,
        project_id: str,
        *,
        tool: str | None = None,
        outcome: str | None = None,
        effect: str | None = None,
        trace_id: str | None = None,
        approval_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """The gateway's decisions, newest first (the first ``limit``)."""
        pairs = (
            ("tool", tool),
            ("outcome", outcome),
            ("effect", effect),
            ("trace_id", trace_id.lower() if trace_id else None),
            ("approval_id", approval_id),
        )
        query = {"project_id": project_id, "limit": str(limit)} | {k: v for k, v in pairs if v}
        return _items(self.request("GET", "/api/v1/policy-decisions", query=query))

    def record_outcome(self, trace_id: str, outcome: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = self.request(
            "POST", f"/api/v1/traces/{trace_id.lower()}/outcome", json_body=dict(outcome)
        )
        return result


def _items(body: Any) -> list[dict[str, Any]]:
    items = body.get("items", []) if isinstance(body, dict) else []
    return [i for i in items if isinstance(i, dict)]


def _tool_endpoint_path(tool: str) -> str:
    return f"/api/v1/tool-endpoints/{urllib.parse.quote(tool, safe='')}"


def _policy_path(policy_id: str, action: str | None = None) -> str:
    path = f"/api/v1/policies/{urllib.parse.quote(policy_id, safe='')}"
    return f"{path}/{action}" if action else path


def _approval_path(approval_id: str, action: str | None = None) -> str:
    path = f"/api/v1/approvals/{urllib.parse.quote(approval_id, safe='')}"
    return f"{path}/{action}" if action else path


def _policy_name(document: str) -> str:
    """``metadata.name`` of a policy document (YAML or JSON)."""
    try:
        import yaml  # optional: only needed to read a YAML document

        doc = yaml.safe_load(document)
    except ImportError:
        doc = json.loads(document)
    name = doc.get("metadata", {}).get("name") if isinstance(doc, dict) else None
    if not isinstance(name, str) or not name:
        raise ValueError("the policy document has no metadata.name")
    return name


def _regression_path(regression_id: str, action: str | None = None) -> str:
    path = f"/api/v1/regressions/{urllib.parse.quote(regression_id, safe='')}"
    return f"{path}/{action}" if action else path


def _idempotency(key: str | None) -> dict[str, str] | None:
    return {"Idempotency-Key": key} if key else None


def _cases(cases: Sequence[str | Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{"scenario": c} if isinstance(c, str) else dict(c) for c in cases]


def _api_error(err: urllib.error.HTTPError) -> APIError:
    code, message, details = "HTTP_ERROR", err.reason if isinstance(err.reason, str) else "request failed", {}
    try:
        body = json.loads(err.read(1 << 20) or b"{}")
        detail = body.get("error", {}) if isinstance(body, dict) else {}
        if isinstance(detail, dict):
            code = str(detail.get("code", code))
            message = str(detail.get("message", message))
            if isinstance(detail.get("details"), dict):
                details = detail["details"]
    except (ValueError, OSError):
        pass
    return APIError(err.code, code, message, details)
