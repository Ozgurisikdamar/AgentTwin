from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import jwt
import pytest

from agenttwin_core.auth import (
    ROLE_PERMISSIONS,
    SCOPE_PERMISSIONS,
    SYSTEM_ORG,
    InvalidToken,
    Permission,
    Principal,
    Role,
    TokenService,
    service_principal,
)

SECRET = "test-internal-secret-0123456789abcdef"
ORG = "0190f3b4-0000-7000-8000-000000000001"
PROJ = "0190f3b4-0000-7000-8000-000000000002"


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_mint_verify_round_trip() -> None:
    ts = TokenService(SECRET)
    p = Principal(org_id=ORG, actor="user:7", role=Role.ENGINEER, email="e@x.dev", project_ids=(PROJ,))
    tok = ts.mint(p, "simulation-service", "req-12345678")
    got, rid = ts.verify(tok, "simulation-service")
    assert got == p
    assert rid == "req-12345678"


def test_audience_issuer_and_signature_are_enforced() -> None:
    ts = TokenService(SECRET)
    tok = ts.mint(service_principal("worker", ORG, PROJ), "trace-service")
    with pytest.raises(InvalidToken):
        ts.verify(tok, "simulation-service")
    with pytest.raises(InvalidToken):
        TokenService("another-secret-0123456789abcdef0123").verify(tok, "trace-service")
    forged = jwt.encode(
        {
            "iss": "someone-else",
            "sub": "user:1",
            "aud": ["trace-service"],
            "exp": 2**40,
            "org": ORG,
            "role": "OWNER",
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(InvalidToken):
        ts.verify(forged, "trace-service")


def test_alg_none_and_other_algorithms_are_rejected() -> None:
    ts = TokenService(SECRET)
    claims = {
        "iss": "agenttwin-internal",
        "sub": "user:1",
        "aud": ["x"],
        "exp": 2**40,
        "org": ORG,
        "role": "OWNER",
    }
    unsigned = jwt.encode(claims, key=None, algorithm="none")  # type: ignore[arg-type]
    with pytest.raises(InvalidToken):
        ts.verify(unsigned, "x")
    hs512 = jwt.encode(claims, SECRET + "0" * 64, algorithm="HS512")
    with pytest.raises(InvalidToken):
        ts.verify(hs512, "x")


def test_expiry_uses_the_injected_clock() -> None:
    clock = Clock(1_800_000_000)
    ts = TokenService(SECRET, clock=clock)
    tok = ts.mint(service_principal("w", ORG), "a")
    clock.t += 64  # within TTL (60 s) + clock skew (5 s)
    ts.verify(tok, "a")
    clock.t += 2
    with pytest.raises(InvalidToken, match="expired"):
        ts.verify(tok, "a")
    clock.t = 1_800_000_000 - 30  # issued "in the future"
    with pytest.raises(InvalidToken, match="not yet valid"):
        ts.verify(tok, "a")


def test_system_org_is_reserved_for_services() -> None:
    ts = TokenService(SECRET)
    with pytest.raises(ValueError, match="system organization"):
        ts.mint(Principal(org_id=SYSTEM_ORG, actor="user:1", role=Role.OWNER), "a")
    forged = jwt.encode(
        {
            "iss": "agenttwin-internal",
            "sub": "user:1",
            "aud": ["a"],
            "exp": 2**40,
            "org": SYSTEM_ORG,
            "role": "OWNER",
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(InvalidToken, match="system organization"):
        ts.verify(forged, "a")


def test_short_secret_is_refused() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        TokenService("short")


def test_rbac_matrix() -> None:
    viewer = Principal(org_id=ORG, actor="user:1", role=Role.VIEWER, project_ids=(PROJ,))
    engineer = Principal(org_id=ORG, actor="user:2", role=Role.ENGINEER, project_ids=(PROJ,))
    reviewer = Principal(org_id=ORG, actor="user:3", role=Role.REVIEWER, all_projects=True)
    assert viewer.can(Permission.READ) and not viewer.can(Permission.SCENARIO_WRITE)
    assert engineer.can(Permission.SCENARIO_WRITE) and engineer.can(Permission.SIMULATION_RUN)
    assert not engineer.can(Permission.RELEASE_OVERRIDE)
    assert not reviewer.can(Permission.SIMULATION_RUN) and reviewer.can(Permission.RELEASE_OVERRIDE)
    ci_key = Principal(org_id=ORG, actor="apikey:1", role=Role.API_KEY, scopes=("ci",), project_ids=(PROJ,))
    trace_key = Principal(org_id=ORG, actor="apikey:2", role=Role.API_KEY, scopes=("traces:write", "bogus"))
    assert ci_key.can(Permission.SIMULATION_RUN)
    assert not trace_key.can(Permission.SIMULATION_RUN) and trace_key.can(Permission.TRACE_WRITE)
    assert viewer.can_access_project(PROJ) and not viewer.can_access_project("other")
    assert reviewer.can_access_project("anything") and not reviewer.can_access_project("")


def test_rbac_matrix_matches_go(go_parity: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]) -> None:
    [res] = go_parity([{"op": "rbac"}])
    go = json.loads(res["output"])
    py_roles = {str(r): sorted(str(p) for p in perms) for r, perms in ROLE_PERMISSIONS.items()}
    py_scopes = {str(s): sorted(str(p) for p in perms) for s, perms in SCOPE_PERMISSIONS.items()}
    assert py_roles == go["roles"]
    assert py_scopes == go["scopes"]


def test_tokens_are_interchangeable_with_go(
    go_parity: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
) -> None:
    ts = TokenService(SECRET)
    principals = [
        Principal(org_id=ORG, actor="user:1", role=Role.OWNER, email="o@x.dev", all_projects=True),
        Principal(
            org_id=ORG, actor="apikey:9", role=Role.API_KEY, scopes=("ci", "read"), project_ids=(PROJ,)
        ),
        service_principal("simulation-worker", ORG, PROJ),
        service_principal("trace-service", SYSTEM_ORG),
    ]
    # Go mints, Python verifies.
    reqs = [
        {
            "op": "mint",
            "secret": SECRET,
            "audience": "simulation-service",
            "request_id": f"rid-{i:08d}",
            "principal": {
                "org": p.org_id,
                "sub": p.actor,
                "email": p.email,
                "role": str(p.role),
                "projects": list(p.project_ids),
                "all_projects": p.all_projects,
                "scopes": list(p.scopes),
            },
        }
        for i, p in enumerate(principals)
    ]
    for i, (p, res) in enumerate(zip(principals, go_parity(reqs), strict=True)):
        assert not res.get("error"), res
        got, rid = ts.verify(res["output"], "simulation-service")
        assert got == p
        assert rid == f"rid-{i:08d}"
    # Python mints, Go verifies.
    tokens = [ts.mint(p, "trace-service", f"py-{i:08d}") for i, p in enumerate(principals)]
    results = go_parity(
        [{"op": "verify", "secret": SECRET, "audience": "trace-service", "input": t} for t in tokens]
    )
    for i, (p, res) in enumerate(zip(principals, results, strict=True)):
        assert not res.get("error"), res
        out = json.loads(res["output"])
        assert out["request_id"] == f"py-{i:08d}"
        gp = out["principal"]
        assert (gp["org"], gp["sub"], gp["role"]) == (p.org_id, p.actor, str(p.role))
        assert tuple(gp.get("projects") or ()) == p.project_ids
        assert tuple(gp.get("scopes") or ()) == p.scopes
        assert bool(gp.get("all_projects")) == p.all_projects
    # Go rejects a Python token for another audience.
    [bad] = go_parity([{"op": "verify", "secret": SECRET, "audience": "control-plane", "input": tokens[0]}])
    assert bad["error"]
