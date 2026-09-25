from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from io import StringIO
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict

from agenttwin_core.auth import Permission, Principal, Role, TokenService
from agenttwin_core.logx import get_logger, setup_logging
from agenttwin_core.telemetry import setup_metrics
from agenttwin_core.web import (
    Health,
    build_app,
    read_json,
    read_model,
    require_project,
    storable,
    storable_value,
)

SECRET = "web-test-internal-secret-0123456789"
ORG = "0190f3b4-0000-7000-8000-000000000001"
PROJ = "0190f3b4-0000-7000-8000-000000000002"


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    count: int = 1


def make_app(health: Health | None = None) -> tuple[FastAPI, TokenService]:
    setup_metrics("web-test", "dev")
    tokens = TokenService(SECRET)
    app = build_app(
        service="web-test", version="dev", health=health or Health(), tokens=tokens, audience="web-test"
    )

    @app.get("/api/v1/projects/{project_id}/things")
    async def things(project_id: str, request: Request) -> dict[str, Any]:
        p = require_project(request, Permission.READ, project_id)
        return {"actor": p.actor}

    @app.post("/api/v1/items")
    async def create(request: Request) -> dict[str, Any]:
        item = await read_model(request, Item, max_bytes=256)
        return item.model_dump()

    @app.post("/api/v1/raw")
    async def raw(request: Request) -> Any:
        return await read_json(request, max_bytes=64)

    @app.get("/api/v1/boom")
    async def boom() -> None:
        raise RuntimeError("secret internals must not leak")

    @app.get("/public/ping")
    async def ping() -> dict[str, str]:
        return {"pong": "yes"}

    return app, tokens


@pytest.fixture
async def client() -> AsyncIterator[tuple[httpx.AsyncClient, TokenService]]:
    app, tokens = make_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://svc"
    ) as c:
        yield c, tokens


def bearer(
    tokens: TokenService, role: Role = Role.ENGINEER, projects: tuple[str, ...] = (PROJ,)
) -> dict[str, str]:
    p = Principal(org_id=ORG, actor="user:1", role=role, project_ids=projects)
    return {"Authorization": "Bearer " + tokens.mint(p, "web-test")}


@pytest.mark.anyio
async def test_requires_internal_token(client: tuple[httpx.AsyncClient, TokenService]) -> None:
    c, tokens = client
    r = await c.get(f"/api/v1/projects/{PROJ}/things")
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["code"] == "UNAUTHENTICATED"
    assert body["error"]["request_id"] == r.headers["x-request-id"]
    r = await c.get(f"/api/v1/projects/{PROJ}/things", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    wrong_aud = tokens.mint(
        Principal(org_id=ORG, actor="user:1", role=Role.OWNER, all_projects=True), "other"
    )
    r = await c.get(f"/api/v1/projects/{PROJ}/things", headers={"Authorization": "Bearer " + wrong_aud})
    assert r.status_code == 401
    r = await c.get(f"/api/v1/projects/{PROJ}/things", headers=bearer(tokens))
    assert r.status_code == 200 and r.json() == {"actor": "user:1"}
    assert (await c.get("/public/ping")).status_code == 200


@pytest.mark.anyio
async def test_other_projects_look_missing(client: tuple[httpx.AsyncClient, TokenService]) -> None:
    c, tokens = client
    r = await c.get("/api/v1/projects/0190f3b4-0000-7000-8000-00000000ffff/things", headers=bearer(tokens))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.anyio
async def test_request_id_is_accepted_or_generated(client: tuple[httpx.AsyncClient, TokenService]) -> None:
    c, _ = client
    r = await c.get("/public/ping", headers={"X-Request-Id": "abc-12345678"})
    assert r.headers["x-request-id"] == "abc-12345678"
    r = await c.get("/public/ping", headers={"X-Request-Id": "bad id with spaces"})
    assert r.headers["x-request-id"] != "bad id with spaces" and len(r.headers["x-request-id"]) == 32
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


@pytest.mark.anyio
async def test_bodies_are_bounded_and_strict(client: tuple[httpx.AsyncClient, TokenService]) -> None:
    c, tokens = client
    h = bearer(tokens)
    r = await c.post("/api/v1/items", headers=h, json={"name": "a", "count": 2})
    assert r.status_code == 200 and r.json() == {"name": "a", "count": 2}
    r = await c.post("/api/v1/items", headers=h, json={"name": "a", "unexpected": True})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "INVALID_REQUEST" and err["details"]["errors"][0]["field"] == "unexpected"
    r = await c.post("/api/v1/items", headers=h, json={"name": "x" * 400})
    assert r.status_code == 413
    r = await c.post("/api/v1/raw", headers={**h, "Content-Type": "text/plain"}, content=b"{}")
    assert r.status_code == 415
    r = await c.post("/api/v1/raw", headers={**h, "Content-Type": "application/json"}, content=b"{nope")
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_JSON"
    r = await c.post("/api/v1/raw", headers={**h, "Content-Type": "application/json"}, content=b"")
    assert r.json()["error"]["code"] == "EMPTY_BODY"


@pytest.mark.anyio
async def test_unhandled_errors_do_not_leak(client: tuple[httpx.AsyncClient, TokenService]) -> None:
    c, tokens = client
    r = await c.get("/api/v1/boom", headers=bearer(tokens))
    assert r.status_code == 500
    assert "secret internals" not in r.text
    err = r.json()["error"]
    assert err["code"] == "INTERNAL"
    # The one id a user can quote is in the body and the header, and they agree.
    assert err["request_id"] and err["request_id"] == r.headers["x-request-id"]
    assert r.headers["x-content-type-options"] == "nosniff"
    text = (await c.get("/metrics")).text
    assert any(
        line.startswith("agenttwin_http_requests_total{")
        and 'route="/api/v1/boom"' in line
        and 'status="500"' in line
        for line in text.splitlines()
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("target", "where"),
    [
        ("/public/ping?q=a%00b", "query"),
        ("/public/ping?q=%FF", "query"),
        ("/public/ping?q=%ED%A0%80", "query"),
        ("/public/ping?ok=1&n%00ame=1", "query"),
        ("/api/v1/projects/a%00b/things", "path"),
        ("/api/v1/projects/%C0%AF/things", "path"),
    ],
)
async def test_text_no_column_stores_is_refused_in_path_and_query(
    client: tuple[httpx.AsyncClient, TokenService], target: str, where: str
) -> None:
    c, tokens = client
    r = await c.get(target, headers=bearer(tokens))
    err = r.json()["error"]
    assert r.status_code == 400 and err["code"] == "INVALID_TEXT", r.text
    assert err["details"]["in"] == where and err["request_id"] == r.headers["x-request-id"]


@pytest.mark.anyio
async def test_text_no_column_stores_is_refused_in_bodies(
    client: tuple[httpx.AsyncClient, TokenService],
) -> None:
    c, tokens = client
    h = {**bearer(tokens), "Content-Type": "application/json"}
    for body in (
        b'{"name":"a\\u0000b"}',
        b'{"name":"\\ud800"}',
        b'{"n\\u0000":"a"}',
        b'{"a":[1,{"b":"\\u0000"}]}',
    ):
        r = await c.post("/api/v1/raw", headers=h, content=body)
        assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_TEXT", (body, r.text)
        assert r.json()["error"]["details"] == {"in": "body"}
    # Every other character is text like any other.
    r = await c.post("/api/v1/items", headers=h, content='{"name":"çağ 注文 🙂 \\\\u0000"}'.encode())
    assert r.status_code == 200 and r.json()["name"] == "çağ 注文 🙂 \\u0000"
    r = await c.get("/public/ping?q=%C3%A7a%C4%9Fr%C4%B1+%E6%B3%A8&x=%27+OR+%271%27%3D%271")
    assert r.status_code == 200


def test_storable() -> None:
    assert storable("çağ 注文 🙂") and storable("")
    assert not storable("a\x00b") and not storable("\ud800")
    assert storable_value({"a": ["b", 1, None, {"c": "d"}]})
    assert not storable_value({"a": ["b", {"c": "d\x00"}]})
    assert not storable_value({"k\x00": 1})


@pytest.mark.anyio
async def test_unknown_route_and_method(client: tuple[httpx.AsyncClient, TokenService]) -> None:
    c, _ = client
    r = await c.get("/public/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    r = await c.delete("/public/ping")
    assert r.status_code == 405 and r.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


@pytest.mark.anyio
async def test_metrics_use_route_templates_only(client: tuple[httpx.AsyncClient, TokenService]) -> None:
    c, tokens = client
    await c.get(f"/api/v1/projects/{PROJ}/things", headers=bearer(tokens))
    await c.get("/api/v1/projects/raw-id-1/things")  # rejected before routing
    text = (await c.get("/metrics")).text
    lines = [line for line in text.splitlines() if line.startswith("agenttwin_http_requests_total{")]
    assert any(
        'route="/api/v1/projects/{project_id}/things"' in line and 'status="200"' in line for line in lines
    )
    assert not any("raw-id-1" in line or PROJ in line for line in lines)
    assert any('route="unmatched"' in line and 'status="401"' in line for line in lines)


@pytest.mark.anyio
async def test_health_readiness_reflects_checks() -> None:
    calls = {"n": 0}

    async def ok() -> None:
        calls["n"] += 1

    async def broken() -> None:
        raise ConnectionError("db down")

    health = Health()
    health.add("postgres", ok)
    app, _ = make_app(health)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://svc"
    ) as c:
        r = await c.get("/health/ready")
        assert r.status_code == 200 and r.json() == {"status": "ready", "checks": {"postgres": "ok"}}
        health.add("rabbitmq", broken)
        r = await c.get("/health/ready")
        assert r.status_code == 503
        assert r.json()["checks"] == {"postgres": "ok", "rabbitmq": "unavailable"}
        health.draining = True
        assert (await c.get("/health/ready")).json()["checks"]["shutdown"] == "draining"
        assert (await c.get("/health/live")).status_code == 200


def test_logs_are_json_with_redaction() -> None:
    buf = StringIO()
    setup_logging("log-test", "debug", stream=buf)
    get_logger("x").info(
        "hello", api_key="atk_abc", authorization="Bearer x", run_id="r1", nested={"password": "p"}
    )
    get_logger("x").debug("dbg")
    line, dbg = buf.getvalue().strip().splitlines()
    entry = json.loads(line)
    assert entry["msg"] == "hello" and entry["service"] == "log-test" and entry["level"] == "INFO"
    assert entry["api_key"] == "[REDACTED]" and entry["authorization"] == "[REDACTED]"
    assert entry["nested"] == {"password": "[REDACTED]"}
    assert entry["run_id"] == "r1"
    assert json.loads(dbg)["level"] == "DEBUG"
    logging.getLogger().handlers.clear()


def test_deliberate_aborts_are_not_logged() -> None:
    import io
    import logging

    from agenttwin_core.errors import DeliberateAbort
    from agenttwin_core.logx import setup_logging

    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    stream = io.StringIO()
    setup_logging("test", "info", stream)
    try:
        log = logging.getLogger("uvicorn.error")
        try:
            raise DeliberateAbort("fault injected")
        except DeliberateAbort as exc:
            log.error("Exception in ASGI application\n", exc_info=exc)
        try:
            raise RuntimeError("real bug")
        except RuntimeError as exc:
            log.error("Exception in ASGI application\n", exc_info=exc)
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        logging.getLogger("uvicorn.access").disabled = False
    lines = [line for line in stream.getvalue().splitlines() if line]
    assert len(lines) == 1 and "real bug" in lines[0]
