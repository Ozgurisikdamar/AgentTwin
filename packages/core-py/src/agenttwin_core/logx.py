"""Structured JSON logging with request/trace correlation and defensive
redaction of credential-like attributes (same fields and rules as ``gokit/logx``).

Usage::

    log = get_logger("simulation.worker")
    log.info("case finished", case_id=case.id, status="PASSED")
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import IO, Any

from opentelemetry import trace

from agenttwin_core.errors import DeliberateAbort

__all__ = ["Log", "get_logger", "is_sensitive_key", "request_id_var", "setup_logging"]

request_id_var: ContextVar[str | None] = ContextVar("agenttwin_request_id", default=None)

# Substring match on the lower-cased key, so "api_key", "x-api-key" and
# "apiKeySecret" are all caught (identical list to gokit/logx).
_SENSITIVE = (
    "password",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "cookie",
    "pepper",
    "private_key",
)
_LEVELS = {"DEBUG": "DEBUG", "INFO": "INFO", "WARNING": "WARN", "ERROR": "ERROR", "CRITICAL": "ERROR"}
_MAX_VALUE = 4000


def is_sensitive_key(key: str) -> bool:
    k = key.lower()
    return any(s in k for s in _SENSITIVE)


def _clean(key: str, value: Any) -> Any:
    if is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, str) and len(value) > _MAX_VALUE:
        return value[:_MAX_VALUE] + "…"
    if isinstance(value, Mapping):
        return {str(k): _clean(str(k), v) for k, v in value.items()}
    return value


class JSONFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            "level": _LEVELS.get(record.levelname, record.levelname),
            "msg": record.getMessage(),
            "service": self.service,
            "logger": record.name,
        }
        attrs = getattr(record, "attrs", None)
        if isinstance(attrs, Mapping):
            for k, v in attrs.items():
                entry[str(k)] = _clean(str(k), v)
        rid = request_id_var.get()
        if rid:
            entry["request_id"] = rid
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            entry["trace_id"] = format(ctx.trace_id, "032x")
            entry["span_id"] = format(ctx.span_id, "016x")
        if record.exc_info:
            entry["error_stack"] = "".join(traceback.format_exception(*record.exc_info))[-8000:]
        return json.dumps(entry, default=str, ensure_ascii=False)


def _drop_deliberate_aborts(record: logging.LogRecord) -> bool:
    """The ASGI server reports an intentionally aborted connection as an
    application exception; it is the fault being injected, not a failure."""
    exc = record.exc_info[1] if record.exc_info else None
    return not isinstance(exc, DeliberateAbort)


def setup_logging(service: str, level: str = "info", stream: IO[str] | None = None) -> None:
    """Routes every logger (including uvicorn and aio-pika) through JSON."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JSONFormatter(service))
    handler.addFilter(_drop_deliberate_aborts)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level.upper() if level.lower() != "warn" else "WARNING")
    # Access logs are written by the service middleware (with route and
    # request id); library chatter stays at WARNING.
    logging.getLogger("uvicorn.access").disabled = True
    for noisy in ("aio_pika", "aiormq", "httpx", "httpcore", "psycopg.pool"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class Log:
    """``logging.Logger`` with slog-style keyword attributes."""

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    def _log(self, level: int, msg: str, attrs: dict[str, Any], exc_info: bool = False) -> None:
        if self._logger.isEnabledFor(level):
            self._logger.log(level, msg, extra={"attrs": attrs}, exc_info=exc_info, stacklevel=3)

    def debug(self, msg: str, **attrs: Any) -> None:
        self._log(logging.DEBUG, msg, attrs)

    def info(self, msg: str, **attrs: Any) -> None:
        self._log(logging.INFO, msg, attrs)

    def warn(self, msg: str, **attrs: Any) -> None:
        self._log(logging.WARNING, msg, attrs)

    def error(self, msg: str, **attrs: Any) -> None:
        self._log(logging.ERROR, msg, attrs)

    def exception(self, msg: str, **attrs: Any) -> None:
        self._log(logging.ERROR, msg, attrs, exc_info=True)


def get_logger(name: str) -> Log:
    return Log(name)
