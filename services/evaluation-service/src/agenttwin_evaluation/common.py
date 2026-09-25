"""Pieces the evaluation service's API modules share: rendering, scoping,
cursors and the audit events of governance actions."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from agenttwin_core import errors
from agenttwin_core.auth import Principal
from agenttwin_core.db import Conn
from agenttwin_core.events import Envelope, write_outbox
from agenttwin_core.ids import valid_uuid
from agenttwin_core.logx import request_id_var
from agenttwin_evaluation.store import SCHEMA, Scope

__all__ = [
    "MAX_CASES",
    "NAME",
    "PRODUCER",
    "TAG",
    "Strict",
    "accessible",
    "audit",
    "check_uuid",
    "decode_name_cursor",
    "name_cursor",
    "pick",
    "scope_of",
    "ts",
]

PRODUCER = "evaluation-service"
MAX_CASES = 500
NAME = r"^[a-z0-9][a-z0-9_-]{0,98}$"
TAG = re.compile(r"^[a-z0-9][a-z0-9_:.-]{0,62}$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def ts(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return value


def pick(row: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    return {k: ts(row.get(k)) for k in keys}


def check_uuid(value: str | None, field: str) -> None:
    if value is not None and not valid_uuid(value):
        raise errors.invalid("INVALID_PARAMETER", f"{field} must be a UUID.", {"field": field})


def accessible(p: Principal, row: Mapping[str, Any] | None) -> bool:
    return (
        row is not None
        and str(row["organization_id"]) == p.org_id
        and p.can_access_project(str(row["project_id"]))
    )


def scope_of(p: Principal, project_id: str | None) -> Scope:
    if project_id:
        check_uuid(project_id, "project_id")
        if not p.can_access_project(project_id):
            raise errors.not_found()
        return Scope(p.org_id, (project_id,))
    if p.all_projects:
        return Scope(p.org_id, None)
    return Scope(p.org_id, tuple(p.project_ids))


def name_cursor(name: str, row_id: str) -> str:
    raw = json.dumps({"n": name, "i": row_id}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_name_cursor(token: str) -> tuple[str, str] | None:
    if not token:
        return None
    try:
        if len(token) > 512:
            raise ValueError
        raw = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        name, row_id = str(raw["n"]), str(raw["i"])
        if not valid_uuid(row_id):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise errors.invalid("INVALID_CURSOR", "Cursor is malformed.") from None
    return name, row_id


async def audit(
    conn: Conn,
    p: Principal,
    project_id: str,
    action: str,
    resource_type: str,
    resource_id: str,
    *,
    reason: str | None = None,
    before_hash: str | None = None,
    after_hash: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """A governance action for the central, hash-chained audit log (the
    control plane consumes ``audit.recorded.v1``), written in the caller's
    transaction so it is recorded exactly when the change commits."""
    rid = request_id_var.get() or None
    await write_outbox(
        conn,
        SCHEMA,
        Envelope.new(
            "audit.recorded.v1",
            PRODUCER,
            p.org_id,
            project_id,
            rid,
            {
                "actor": p.actor,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "timestamp": ts(datetime.now(UTC)),
                "request_id": rid,
                "before_hash": before_hash,
                "after_hash": after_hash,
                "reason": reason,
                "metadata": dict(metadata or {}),
            },
        ),
    )
