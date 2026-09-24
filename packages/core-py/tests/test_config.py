from __future__ import annotations

import pytest

from agenttwin_core.config import ConfigError, Environment, Loader


def test_defaults_and_typed_values() -> None:
    env = {
        "PORT": "8084",
        "RATIO": "0.5",
        "FLAG": "yes",
        "WAIT": "250ms",
        "LIST": " a, b ,,c ",
        "MODE": "strict",
    }
    lo = Loader(env)
    assert lo.env is Environment.DEVELOPMENT
    assert lo.integer("PORT", 1, 1, 65535) == 8084
    assert lo.number("RATIO", 1.0, 0, 1) == 0.5
    assert lo.boolean("FLAG", False) is True
    assert lo.duration("WAIT", 5) == pytest.approx(0.25)
    assert lo.duration("MISSING", 5) == 5
    assert lo.str_list("LIST") == ["a", "b", "c"]
    assert lo.one_of("MODE", "strict", "strict", "approximate") == "strict"
    assert lo.string("EMPTY", "dflt") == "dflt"
    lo.raise_for_errors()


def test_all_problems_are_reported_at_once_and_sorted() -> None:
    lo = Loader({"APP_ENV": "staging", "PORT": "99999", "FLAG": "maybe", "WAIT": "5 minutes", "N": "x"})
    lo.integer("PORT", 1, 1, 65535)
    lo.boolean("FLAG", False)
    lo.duration("WAIT", 1)
    lo.integer("N", 1, 0, 10)
    lo.required("DATABASE_URL")
    with pytest.raises(ConfigError) as exc:
        lo.raise_for_errors()
    problems = exc.value.problems
    assert problems == sorted(problems)
    assert len(problems) == 6
    assert any(p.startswith("APP_ENV:") for p in problems)
    assert any(p.startswith("DATABASE_URL: is required") for p in problems)


def test_production_refuses_placeholder_and_short_secrets() -> None:
    lo = Loader({"APP_ENV": "production", "A": "change-me-dev-secret-0123456789abcdef0123", "B": "short"})
    lo.secret("A", 32)
    lo.secret("B", 32)
    assert lo.errors == [
        "A: uses a development placeholder value; set a real secret in production",
        "B: must be at least 32 bytes in production",
    ]


def test_development_accepts_placeholders() -> None:
    lo = Loader({"A": "change-me"})
    assert lo.secret("A", 32) == "change-me"
    lo.raise_for_errors()


def test_parsed_turns_value_errors_into_problems() -> None:
    def parse(v: str) -> int:
        if not v.startswith("n"):
            raise ValueError("must start with n")
        return len(v)

    lo = Loader({"GOOD": "nnn", "BAD": "x"})
    assert lo.parsed("GOOD", 0, parse) == 3
    assert lo.parsed("BAD", 7, parse) == 7
    assert lo.errors == ["BAD: must start with n"]
