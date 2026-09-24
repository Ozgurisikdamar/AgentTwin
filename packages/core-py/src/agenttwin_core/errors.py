"""API errors rendered as ``{"error": {"code", "message", "details", "request_id"}}``
(the envelope every AgentTwin service uses, spec §37)."""

from __future__ import annotations

from typing import Any

__all__ = [
    "APIError",
    "DeliberateAbort",
    "conflict",
    "forbidden",
    "invalid",
    "not_found",
    "payload_too_large",
    "unauthenticated",
    "unavailable",
]


class DeliberateAbort(Exception):
    """Raised to make the server close a connection on purpose (fault
    injection: dropped connections, partial responses). It is not logged as
    an error by the HTTP stack."""


class APIError(Exception):
    """An error with an HTTP status and a stable machine-readable code."""

    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def with_details(self, details: dict[str, Any]) -> APIError:
        return APIError(self.status, self.code, self.message, details)

    def body(self, request_id: str | None) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message, "request_id": request_id or ""}
        if self.details:
            err["details"] = self.details
        return {"error": err}


def not_found(message: str = "The requested resource does not exist.") -> APIError:
    return APIError(404, "NOT_FOUND", message)


def unauthenticated() -> APIError:
    return APIError(401, "UNAUTHENTICATED", "Authentication is required.")


def forbidden(permission: str | None = None) -> APIError:
    details = {"required_permission": permission} if permission else None
    return APIError(403, "FORBIDDEN", "You do not have permission to perform this action.", details)


def conflict(code: str, message: str, details: dict[str, Any] | None = None) -> APIError:
    return APIError(409, code, message, details)


def invalid(code: str, message: str, details: dict[str, Any] | None = None) -> APIError:
    return APIError(400, code, message, details)


def payload_too_large() -> APIError:
    return APIError(413, "PAYLOAD_TOO_LARGE", "The request body is too large.")


def unavailable(message: str = "A dependency is temporarily unavailable. Retry later.") -> APIError:
    return APIError(503, "UNAVAILABLE", message)
