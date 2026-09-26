"""HTTP conventions of the Python services, matching ``gokit/httpx``:

* ``{"error": {"code", "message", "details", "request_id"}}`` for every error;
* ``X-Request-Id`` accepted when well formed, generated otherwise, echoed;
* security headers, access log and ``agenttwin_http_*`` metrics per route;
* ``/health/live``, ``/health/ready`` (dependency checks) and ``/metrics``;
* internal service JWT verification for ``/api`` and ``/internal`` routes;
* bounded, strict JSON bodies (unknown fields are rejected);
* text PostgreSQL cannot store (a NUL character, bytes that are not UTF-8, a
  lone surrogate) refused with 400 ``INVALID_TEXT`` in the path, the query
  and JSON bodies, instead of failing inside the database as a 500.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agenttwin_core import errors
from agenttwin_core.auth import InvalidToken, Permission, Principal, TokenService
from agenttwin_core.db import is_unavailable
from agenttwin_core.errors import APIError, DeliberateAbort
from agenttwin_core.ids import new_id
from agenttwin_core.logx import get_logger, request_id_var
from agenttwin_core.telemetry import metrics

__all__ = [
    "DEFAULT_MAX_BODY",
    "REQUEST_ID_HEADER",
    "Health",
    "build_app",
    "invalid_text",
    "principal",
    "read_json",
    "read_model",
    "require",
    "require_project",
    "storable",
    "storable_value",
]

REQUEST_ID_HEADER = "X-Request-Id"
DEFAULT_MAX_BODY = 1 << 20
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_log = get_logger("agenttwin.http")

Check = Callable[[], Awaitable[None]]


@dataclass
class Health:
    """Readiness checkers; each must answer within ``timeout_s``."""

    checks: dict[str, Check] = field(default_factory=dict)
    timeout_s: float = 2.0
    draining: bool = False

    def add(self, name: str, check: Check) -> None:
        self.checks[name] = check

    async def ready(self) -> tuple[bool, dict[str, str]]:
        results: dict[str, str] = {}
        ok = not self.draining
        if self.draining:
            results["shutdown"] = "draining"
        for name, check in self.checks.items():
            try:
                await asyncio.wait_for(check(), timeout=self.timeout_s)
                results[name] = "ok"
            except Exception:  # noqa: BLE001 - any failure means "not ready"
                ok = False
                results[name] = "unavailable"
        return ok, results


# The Retry-After (seconds) of a 503 (gokit's httpx.RetryAfterUnavailable).
RETRY_AFTER_UNAVAILABLE = "2"


def _error_response(err: APIError) -> JSONResponse:
    headers = {"Retry-After": RETRY_AFTER_UNAVAILABLE} if err.status == 503 else None
    return JSONResponse(err.body(request_id_var.get()), status_code=err.status, headers=headers)


def _unexpected(exc: Exception, path: str) -> APIError:
    """The answer to an exception no handler turned into an APIError: 503 when
    the database is unreachable (retry later), otherwise 500 without internals."""
    if is_unavailable(exc):
        _log.warn("database unavailable", path=path, error=type(exc).__name__)
        return errors.unavailable()
    _log.exception("unhandled error", path=path, error=type(exc).__name__)
    return APIError(
        500, "INTERNAL", "An internal error occurred. Use the request id when contacting support."
    )


INVALID_TEXT_MESSAGE = "Text in a request must be valid UTF-8 and cannot contain a NUL character."


def invalid_text(where: str, parameter: str | None = None) -> APIError:
    details: dict[str, Any] = {"in": where}
    if parameter is not None:
        details["parameter"] = parameter
    return errors.invalid("INVALID_TEXT", INVALID_TEXT_MESSAGE, details)


def _storable_bytes(raw: bytes) -> bool:
    """Percent-decoded ``raw`` is UTF-8 without a NUL character."""
    decoded = urllib.parse.unquote_to_bytes(raw)
    if b"\x00" in decoded:
        return False
    try:
        decoded.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def storable(text: str) -> bool:
    """A string PostgreSQL stores: no NUL character and no lone surrogate
    (which JSON's ``\\ud800`` escape produces and UTF-8 cannot encode)."""
    if "\x00" in text:
        return False
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def storable_value(value: Any) -> bool:
    """Every string of a decoded JSON value, keys included, is ``storable``."""
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, str):
            if not storable(v):
                return False
        elif isinstance(v, dict):
            for k, e in v.items():
                if isinstance(k, str) and not storable(k):
                    return False
                stack.append(e)
        elif isinstance(v, list):
            stack.extend(v)
    return True


def _unstorable_request_text(scope: Scope) -> APIError | None:
    raw_path = scope.get("raw_path")
    if isinstance(raw_path, bytes):
        # Some clients' ASGI adapters (httpx's) put the query in raw_path too.
        if not _storable_bytes(raw_path.partition(b"?")[0]):
            return invalid_text("path")
    elif not storable(str(scope.get("path", ""))):
        return invalid_text("path")
    query = scope.get("query_string", b"")
    for pair in query.split(b"&") if query else ():
        name, _, value = pair.partition(b"=")
        if not (_storable_bytes(name) and _storable_bytes(value)):
            shown = urllib.parse.unquote_to_bytes(name.replace(b"+", b" ")).decode("utf-8", "replace")
            return invalid_text("query", shown.replace("\x00", "\ufffd"))
    return None


class _Middleware:
    """Request id, security headers, access log and metrics (pure ASGI, so
    streaming responses and context variables behave)."""

    def __init__(self, app: ASGIApp, service: str) -> None:
        self.app = app
        self.service = service

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        rid = headers.get("x-request-id", "")
        if not _VALID_REQUEST_ID.match(rid):
            rid = new_id().replace("-", "")
        token = request_id_var.set(rid)
        start = time.perf_counter()
        status = 500
        started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status, started
            if message["type"] == "http.response.start":
                started = True
                status = int(message["status"])
                extra = [
                    (b"x-request-id", rid.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"cache-control", b"no-store"),
                ]
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        try:
            bad_text = _unstorable_request_text(scope)
            if bad_text is not None:
                await _error_response(bad_text)(scope, receive, send_wrapper)
            else:
                await self.app(scope, receive, send_wrapper)
        except DeliberateAbort:
            raise  # the server must drop the connection (fault injection)
        except Exception as exc:
            # Answered here, inside the request id's scope: the framework's
            # last-resort handler runs outside this middleware, where the
            # request id is gone and the response lacks its headers.
            if started:
                raise
            await _error_response(_unexpected(exc, scope.get("path", "")))(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            path = scope.get("path", "")
            route = scope.get("route")
            # Only route templates become labels: a raw path would create one
            # time series per id (requests rejected before routing included).
            template = getattr(route, "path", None) or "unmatched"
            method = scope.get("method", "GET")
            m = metrics()
            if m is not None and path != "/metrics" and not path.startswith("/health/"):
                m.observe_http(method, template, status, elapsed)
            if not path.startswith("/health/") and path != "/metrics":
                level = _log.warn if status >= 500 else _log.info
                level(
                    "http request",
                    method=method,
                    route=template,
                    status=status,
                    duration_ms=round(elapsed * 1000, 2),
                )
            request_id_var.reset(token)


class _InternalAuth:
    """Verifies the internal service JWT on authenticated prefixes and stores
    the principal in the request state (``request.state.principal``)."""

    def __init__(self, app: ASGIApp, tokens: TokenService, audience: str, prefixes: tuple[str, ...]) -> None:
        self.app = app
        self.tokens = tokens
        self.audience = audience
        self.prefixes = prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith(self.prefixes):
            await self.app(scope, receive, send)
            return
        auth = ""
        for k, v in scope.get("headers", []):
            if k.lower() == b"authorization":
                auth = v.decode("latin-1")
                break
        if not auth.lower().startswith("bearer "):
            await _error_response(errors.unauthenticated())(scope, receive, send)
            return
        try:
            p, rid = self.tokens.verify(auth[7:].strip(), self.audience)
        except InvalidToken:
            await _error_response(errors.unauthenticated())(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        state["principal"] = p
        state["token_request_id"] = rid
        await self.app(scope, receive, send)


def build_app(
    *,
    service: str,
    version: str,
    health: Health,
    tokens: TokenService | None,
    audience: str,
    authenticated_prefixes: tuple[str, ...] = ("/api/", "/internal/"),
) -> FastAPI:
    """A FastAPI app with the standard behavior; routes are added by the caller."""
    # The API contracts are the documents in packages/contracts/openapi,
    # checked against the running service (ADR-0021); FastAPI's generated
    # schema would describe handlers that read their bodies themselves as
    # taking no input, so it is not served.
    app = FastAPI(title=service, version=version, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(APIError)
    async def _api_error(_: Request, exc: APIError) -> JSONResponse:
        return _error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(
            errors.invalid(
                "INVALID_REQUEST", "The request is invalid.", {"errors": _pydantic_errors(exc.errors())}
            )
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return _error_response(errors.not_found())
        if exc.status_code == 405:
            return _error_response(APIError(405, "METHOD_NOT_ALLOWED", "Method not allowed."))
        return _error_response(APIError(exc.status_code, "HTTP_ERROR", str(exc.detail)))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, DeliberateAbort):
            return _error_response(
                APIError(
                    500, "INTERNAL", "An internal error occurred. Use the request id when contacting support."
                )
            )
        return _error_response(_unexpected(exc, request.url.path))

    @app.get("/health/live", include_in_schema=False)
    async def _live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready", include_in_schema=False)
    async def _ready() -> JSONResponse:
        ok, results = await health.ready()
        return JSONResponse(
            {"status": "ready" if ok else "not_ready", "checks": results}, status_code=200 if ok else 503
        )

    @app.get("/metrics", include_in_schema=False)
    async def _metrics() -> Response:
        m = metrics()
        body = generate_latest(m.registry) if m is not None else b""
        return Response(body, media_type=CONTENT_TYPE_LATEST)

    if tokens is not None:
        app.add_middleware(_InternalAuth, tokens=tokens, audience=audience, prefixes=authenticated_prefixes)
    app.add_middleware(_Middleware, service=service)
    return app


def _pydantic_errors(items: Any) -> list[dict[str, Any]]:
    out = []
    for e in list(items)[:20]:
        loc = ".".join(str(p) for p in e.get("loc", ()) if p != "body")
        out.append({"field": loc, "message": str(e.get("msg", "invalid"))})
    return out


def principal(request: Request) -> Principal:
    p = getattr(request.state, "principal", None)
    if not isinstance(p, Principal):
        raise errors.unauthenticated()
    return p


def require(request: Request, perm: Permission) -> Principal:
    p = principal(request)
    if not p.can(perm):
        raise errors.forbidden(str(perm))
    return p


def require_project(request: Request, perm: Permission, project_id: str) -> Principal:
    """Permission plus project access. Projects of other organizations or
    outside the caller's scope look like missing resources (no probing)."""
    p = require(request, perm)
    if not p.can_access_project(project_id):
        raise errors.not_found()
    return p


async def read_json(request: Request, max_bytes: int = DEFAULT_MAX_BODY) -> Any:
    """Reads a bounded JSON body (``application/json`` only)."""
    ctype = request.headers.get("content-type", "")
    if ctype and not ctype.lower().startswith("application/json"):
        raise APIError(415, "UNSUPPORTED_MEDIA_TYPE", "Content-Type must be application/json.")
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        raise errors.payload_too_large()
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise errors.payload_too_large()
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw.strip():
        raise errors.invalid("EMPTY_BODY", "A JSON request body is required.")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as err:
        raise errors.invalid(
            "INVALID_JSON", "The request body is not valid JSON.", {"reason": str(err)}
        ) from None
    if not storable_value(value):
        raise invalid_text("body")
    return value


async def read_model[M: BaseModel](request: Request, model: type[M], max_bytes: int = DEFAULT_MAX_BODY) -> M:
    """Reads and validates a body against a pydantic model (extra fields forbidden
    by the model config)."""
    data = await read_json(request, max_bytes)
    try:
        return model.model_validate(data)
    except ValidationError as err:
        raise errors.invalid(
            "INVALID_REQUEST", "The request is invalid.", {"errors": _pydantic_errors(err.errors())}
        ) from None


def query_int(params: Mapping[str, str], key: str, default: int, minimum: int, maximum: int) -> int:
    raw = params.get(key)
    if raw is None or raw == "":
        return default
    try:
        n = int(raw)
    except ValueError:
        raise errors.invalid("INVALID_PARAMETER", f"{key} must be an integer.", {"field": key}) from None
    if not minimum <= n <= maximum:
        raise errors.invalid(
            "INVALID_PARAMETER", f"{key} must be between {minimum} and {maximum}.", {"field": key}
        )
    return n
