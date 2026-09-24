"""Typed configuration from environment variables (mirrors ``gokit/config``).

Every service builds its configuration through a :class:`Loader` so all
problems are reported at once ("missing X, invalid Y") instead of one per
restart, and so production refuses the development placeholders that
``.env.example`` ships (values starting with ``change-me``).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from enum import StrEnum

__all__ = ["DEV_SECRET_PREFIX", "ConfigError", "Environment", "Loader"]

DEV_SECRET_PREFIX = "change-me"  # noqa: S105 - the placeholder marker, not a secret

_DURATION = re.compile(r"^(?P<num>\d+(?:\.\d+)?)(?P<unit>ms|s|m|h)$")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class ConfigError(ValueError):
    """All configuration problems of one process, sorted."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = sorted(problems)
        super().__init__("invalid configuration:\n  - " + "\n  - ".join(self.problems))


class Loader:
    """Reads variables, accumulating errors until :meth:`raise_for_errors`."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        self._errors: list[str] = []
        raw = self.string("APP_ENV", Environment.DEVELOPMENT).lower()
        try:
            self.env = Environment(raw)
        except ValueError:
            self._fail("APP_ENV", f"must be one of development|test|production, got {raw!r}")
            self.env = Environment.DEVELOPMENT

    # -- helpers -------------------------------------------------------------

    def _fail(self, key: str, message: str) -> None:
        self._errors.append(f"{key}: {message}")

    def _get(self, key: str) -> str | None:
        value = self._environ.get(key)
        if value is None:
            return None
        value = value.strip()
        return value or None

    # -- typed getters -------------------------------------------------------

    def string(self, key: str, default: str = "") -> str:
        value = self._get(key)
        return default if value is None else value

    def required(self, key: str) -> str:
        value = self._get(key)
        if value is None:
            self._fail(key, "is required")
            return ""
        return value

    def secret(self, key: str, min_len: int) -> str:
        """A required secret; production rejects placeholders and short values."""
        value = self.required(key)
        if value and self.env is Environment.PRODUCTION:
            if value.lower().startswith(DEV_SECRET_PREFIX):
                self._fail(key, "uses a development placeholder value; set a real secret in production")
            if len(value.encode()) < min_len:
                self._fail(key, f"must be at least {min_len} bytes in production")
        return value

    def integer(self, key: str, default: int, minimum: int, maximum: int) -> int:
        value = self._get(key)
        if value is None:
            return default
        try:
            n = int(value)
        except ValueError:
            self._fail(key, f"must be an integer, got {value!r}")
            return default
        if not minimum <= n <= maximum:
            self._fail(key, f"must be between {minimum} and {maximum}, got {n}")
            return default
        return n

    def number(self, key: str, default: float, minimum: float, maximum: float) -> float:
        value = self._get(key)
        if value is None:
            return default
        try:
            f = float(value)
        except ValueError:
            self._fail(key, f"must be a number, got {value!r}")
            return default
        if not minimum <= f <= maximum:
            self._fail(key, f"must be between {minimum:g} and {maximum:g}, got {f:g}")
            return default
        return f

    def boolean(self, key: str, default: bool) -> bool:
        value = self._get(key)
        if value is None:
            return default
        low = value.lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        self._fail(key, f"must be a boolean, got {value!r}")
        return default

    def duration(self, key: str, default_seconds: float) -> float:
        """A duration like ``250ms``, ``5s``, ``2m`` or ``1h``, in seconds."""
        value = self._get(key)
        if value is None:
            return default_seconds
        m = _DURATION.match(value)
        if not m:
            self._fail(key, f"must be a non-negative duration like 5s, got {value!r}")
            return default_seconds
        return float(m["num"]) * _UNIT_SECONDS[m["unit"]]

    def str_list(self, key: str, default: list[str] | None = None) -> list[str]:
        value = self._get(key)
        if value is None:
            return list(default or [])
        return [p.strip() for p in value.split(",") if p.strip()]

    def one_of(self, key: str, default: str, *allowed: str) -> str:
        value = self.string(key, default)
        if value not in allowed:
            self._fail(key, f"must be one of {'|'.join(allowed)}, got {value!r}")
            return default
        return value

    def parsed[T](self, key: str, default: T, parse: Callable[[str], T]) -> T:
        """A value converted by ``parse``; a ValueError becomes a config problem."""
        value = self._get(key)
        if value is None:
            return default
        try:
            return parse(value)
        except ValueError as err:
            self._fail(key, str(err))
            return default

    def check(self, condition: bool, key: str, message: str) -> None:
        if not condition:
            self._fail(key, message)

    @property
    def errors(self) -> list[str]:
        return sorted(self._errors)

    def raise_for_errors(self) -> None:
        if self._errors:
            raise ConfigError(self._errors)
