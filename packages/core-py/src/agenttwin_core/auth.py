"""Principals, RBAC and the internal service token (ADR-0009).

This is the Python twin of ``gokit/authn``. The role/scope permission matrix
and the token format are checked against the Go implementation by
differential tests (``tests/test_auth.py``), so a permission cannot be
granted in one language and denied in the other.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import jwt

from agenttwin_core.ids import new_id

__all__ = [
    "INTERNAL_ISSUER",
    "INTERNAL_TOKEN_TTL_S",
    "ROLE_PERMISSIONS",
    "SCOPE_PERMISSIONS",
    "SYSTEM_ORG",
    "InvalidToken",
    "Permission",
    "Principal",
    "Role",
    "Scope",
    "TokenService",
    "service_principal",
]

INTERNAL_ISSUER = "agenttwin-internal"
INTERNAL_TOKEN_TTL_S = 60
SYSTEM_ORG = "_system"
_CLOCK_SKEW_S = 5


class Role(StrEnum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    ENGINEER = "ENGINEER"
    REVIEWER = "REVIEWER"
    VIEWER = "VIEWER"
    API_KEY = "API_KEY"
    SERVICE = "SERVICE"


class Permission(StrEnum):
    READ = "read"
    SETTINGS_READ = "settings.read"
    SETTINGS_WRITE = "settings.write"
    APIKEY_MANAGE = "apikey.manage"
    AGENT_WRITE = "agent.write"
    SCENARIO_WRITE = "scenario.write"
    SIMULATION_RUN = "simulation.run"
    EVAL_RUN = "eval.run"
    RELEASE_WRITE = "release.write"
    RELEASE_OVERRIDE = "release.override"
    REVIEW_WRITE = "review.write"
    REGRESSION_PROMOTE = "regression.promote"
    APPROVAL_DECIDE = "approval.decide"
    POLICY_WRITE = "policy.write"
    POLICY_ACTIVATE = "policy.activate"
    POLICY_TEST = "policy.test"
    TRACE_WRITE = "trace.write"
    RUNTIME_INVOKE = "runtime.invoke"
    GRAPH_WRITE = "graph.write"


class Scope(StrEnum):
    TRACES_WRITE = "traces:write"
    RUNTIME_INVOKE = "runtime:invoke"
    READ = "read"
    CI = "ci"


P = Permission
_ADMINISTRATIVE = frozenset(
    {
        P.READ,
        P.SETTINGS_READ,
        P.SETTINGS_WRITE,
        P.APIKEY_MANAGE,
        P.AGENT_WRITE,
        P.SCENARIO_WRITE,
        P.SIMULATION_RUN,
        P.EVAL_RUN,
        P.RELEASE_WRITE,
        P.RELEASE_OVERRIDE,
        P.REVIEW_WRITE,
        P.REGRESSION_PROMOTE,
        P.APPROVAL_DECIDE,
        P.POLICY_WRITE,
        P.POLICY_ACTIVATE,
        P.POLICY_TEST,
        P.GRAPH_WRITE,
    }
)

# The explicit RBAC matrix (identical to gokit/authn.rolePermissions).
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.OWNER: _ADMINISTRATIVE,
    Role.ADMIN: _ADMINISTRATIVE,
    Role.ENGINEER: frozenset(
        {
            P.READ,
            P.SETTINGS_READ,
            P.AGENT_WRITE,
            P.SCENARIO_WRITE,
            P.SIMULATION_RUN,
            P.EVAL_RUN,
            P.RELEASE_WRITE,
            P.REVIEW_WRITE,
            P.POLICY_TEST,
            P.GRAPH_WRITE,
        }
    ),
    Role.REVIEWER: frozenset(
        {P.READ, P.REVIEW_WRITE, P.REGRESSION_PROMOTE, P.APPROVAL_DECIDE, P.RELEASE_OVERRIDE, P.POLICY_TEST}
    ),
    Role.VIEWER: frozenset({P.READ}),
}

SCOPE_PERMISSIONS: dict[Scope, frozenset[Permission]] = {
    Scope.TRACES_WRITE: frozenset({P.TRACE_WRITE}),
    Scope.RUNTIME_INVOKE: frozenset({P.RUNTIME_INVOKE}),
    Scope.READ: frozenset({P.READ}),
    Scope.CI: frozenset(
        {
            P.READ,
            P.AGENT_WRITE,
            P.SCENARIO_WRITE,
            P.SIMULATION_RUN,
            P.EVAL_RUN,
            P.RELEASE_WRITE,
            P.POLICY_TEST,
        }
    ),
}


@dataclass(frozen=True)
class Principal:
    """An authenticated caller: ``user:<id>``, ``apikey:<id>`` or ``service:<name>``."""

    org_id: str
    actor: str
    role: Role
    email: str = ""
    project_ids: tuple[str, ...] = field(default_factory=tuple)
    all_projects: bool = False
    scopes: tuple[str, ...] = field(default_factory=tuple)

    def can(self, perm: Permission) -> bool:
        if self.role is Role.API_KEY:
            for s in self.scopes:
                try:
                    if perm in SCOPE_PERMISSIONS[Scope(s)]:
                        return True
                except ValueError:
                    continue
            return False
        if self.role is Role.SERVICE:
            # Service principals act inside an already-authorized flow and are
            # limited to the organization/projects embedded in the token.
            return True
        return perm in ROLE_PERMISSIONS.get(self.role, frozenset())

    def can_access_project(self, project_id: str) -> bool:
        """Project ids are UUIDs, which are case-insensitive (RFC 9562): a
        request may name a project in either case."""
        if not project_id:
            return False
        wanted = project_id.lower()
        return self.all_projects or any(p.lower() == wanted for p in self.project_ids)


def service_principal(service: str, org_id: str, *project_ids: str) -> Principal:
    """The principal of event-driven service-to-service calls."""
    return Principal(
        org_id=org_id, actor=f"service:{service}", role=Role.SERVICE, project_ids=tuple(project_ids)
    )


class InvalidToken(Exception):
    """The internal token is missing, malformed, expired or not for us."""


class TokenService:
    """Mints and verifies 60-second HS256 internal tokens with a shared key."""

    def __init__(self, secret: str, *, clock: Any = time.time) -> None:
        if len(secret.encode()) < 32:
            raise ValueError("internal token secret must be at least 32 bytes")
        self._key = secret.encode()
        self._clock = clock

    def mint(self, p: Principal, audience: str, request_id: str = "") -> str:
        if not p.org_id or not p.actor or not p.role:
            raise ValueError("mint: principal requires org, actor and role")
        if p.org_id == SYSTEM_ORG and p.role is not Role.SERVICE:
            raise ValueError("mint: only service principals may use the system organization")
        now = int(self._clock())
        claims: dict[str, Any] = {
            "iss": INTERNAL_ISSUER,
            "sub": p.actor,
            "aud": [audience],
            "iat": now,
            "nbf": now - _CLOCK_SKEW_S,
            "exp": now + INTERNAL_TOKEN_TTL_S,
            "jti": new_id(),
            "org": p.org_id,
            "role": str(p.role),
        }
        if p.email:
            claims["email"] = p.email
        if p.project_ids:
            claims["projects"] = list(p.project_ids)
        if p.all_projects:
            claims["all_projects"] = True
        if p.scopes:
            claims["scopes"] = list(p.scopes)
        if request_id:
            claims["rid"] = request_id
        return jwt.encode(claims, self._key, algorithm="HS256")

    def verify(self, token: str, audience: str) -> tuple[Principal, str]:
        """Returns the principal and the request id carried by the token."""
        try:
            # Time claims are checked below against the injectable clock
            # (PyJWT would always use the wall clock).
            claims = jwt.decode(
                token,
                self._key,
                algorithms=["HS256"],
                audience=audience,
                issuer=INTERNAL_ISSUER,
                options={
                    "require": ["exp", "iss", "aud", "sub"],
                    "verify_exp": False,
                    "verify_nbf": False,
                    "verify_iat": False,
                },
            )
        except jwt.PyJWTError as err:
            raise InvalidToken(f"invalid internal token: {err}") from None
        now = float(self._clock())
        exp, nbf = claims.get("exp"), claims.get("nbf")
        if not isinstance(exp, int | float) or isinstance(exp, bool) or now > exp + _CLOCK_SKEW_S:
            raise InvalidToken("invalid internal token: expired")
        if nbf is not None and (not isinstance(nbf, int | float) or now + _CLOCK_SKEW_S < nbf):
            raise InvalidToken("invalid internal token: not yet valid")
        org, sub, role = claims.get("org"), claims.get("sub"), claims.get("role")
        if not (isinstance(org, str) and org and isinstance(sub, str) and sub and isinstance(role, str)):
            raise InvalidToken("invalid internal token: missing claims")
        try:
            r = Role(role)
        except ValueError:
            raise InvalidToken("invalid internal token: unknown role") from None
        if org == SYSTEM_ORG and r is not Role.SERVICE:
            raise InvalidToken("invalid internal token: system organization requires a service principal")
        projects = claims.get("projects") or []
        scopes = claims.get("scopes") or []
        if not isinstance(projects, list) or not isinstance(scopes, list):
            raise InvalidToken("invalid internal token: malformed claims")
        p = Principal(
            org_id=org,
            actor=sub,
            role=r,
            email=str(claims.get("email") or ""),
            project_ids=tuple(str(x) for x in projects),
            all_projects=bool(claims.get("all_projects", False)),
            scopes=tuple(str(x) for x in scopes),
        )
        return p, str(claims.get("rid") or "")
