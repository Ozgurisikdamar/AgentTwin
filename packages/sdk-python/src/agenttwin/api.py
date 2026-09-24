"""A small client for the AgentTwin REST API (standard library only).

Authenticates with a project API key (``X-AgentTwin-Api-Key``). Covers what
an agent codebase or its CI needs: finding the project, registering agent
manifests, tool twins and scenarios, running simulations and recording
outcomes. Errors carry the API's machine-readable code (``APIError.code``)
and never include the key.
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
    ) -> dict[str, Any]:
        """Queues a simulation run of an agent version against its scenarios
        (all of them unless ``scenarios`` or ``tags`` narrow the selection)."""
        body: dict[str, Any] = {"project_id": project_id, "agent": agent, "agent_version": agent_version}
        if scenarios is not None:
            body["scenarios"] = list(scenarios)
        if tags is not None:
            body["tags"] = list(tags)
        if seed is not None:
            body["seed"] = seed
        if release_id is not None:
            body["release_id"] = release_id
        result: dict[str, Any] = self.request("POST", "/api/v1/simulations", json_body=body)
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

    def record_outcome(self, trace_id: str, outcome: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = self.request(
            "POST", f"/api/v1/traces/{trace_id.lower()}/outcome", json_body=dict(outcome)
        )
        return result


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
