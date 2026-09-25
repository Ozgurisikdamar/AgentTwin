from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agenttwin.redaction import RedactionConfig, Redactor, truncate
from sdk_testutil import load_fixture, texty

FIXTURE = load_fixture("redaction.json")


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["name"])
def test_matches_shared_fixture(case: dict[str, Any]) -> None:
    strategy = case.get("strategy", "mask")
    red = Redactor.all(strategy) if case["mode"] == "all" else Redactor.secrets(strategy)
    out, _ = red.text(case["input"])
    assert out == case["output"]


@settings(max_examples=500, deadline=None)
@given(texty, st.sampled_from(["mask", "hash"]), st.booleans())
def test_redaction_is_idempotent_and_never_raises(s: str, strategy: str, pii: bool) -> None:
    red = RedactionConfig(strategy=strategy, pii=pii).redactor()  # type: ignore[arg-type]
    once, _ = red.text(s)
    assert once is not None
    twice, changed = red.text(once)
    assert twice == once
    assert not changed


def test_drop_strategy_drops_whole_value() -> None:
    red = RedactionConfig(strategy="drop").redactor()
    assert red.text("mail jane@example.com") == (None, True)
    assert red.text("nothing to see") == ("nothing to see", False)


def test_json_paths_and_leaves() -> None:
    value = {
        "customer": {"email": "a@b.co", "name": "Jane", "note": "call +44 20 7946 0958"},
        "items": [{"card": "4111111111111111", "sku": "A"}, {"card": "x", "sku": "B"}],
        "email_key_is_not_content": 1,
    }
    red = RedactionConfig(json_paths=("$.customer.name", "$.items[*].card")).redactor()
    out, changed = red.value(value)
    assert changed
    assert out["customer"]["name"] == "[REDACTED:field]"
    assert out["customer"]["email"] == "[REDACTED:email]"
    assert out["customer"]["note"] == "call [REDACTED:phone]"
    assert [i["card"] for i in out["items"]] == ["[REDACTED:field]", "[REDACTED:field]"]
    assert [i["sku"] for i in out["items"]] == ["A", "B"]
    assert value["customer"]["name"] == "Jane", "input must not be mutated"

    dropped, _ = RedactionConfig(strategy="drop", json_paths=("$.customer",)).redactor().value(value)
    assert "customer" not in dropped

    hashed, _ = RedactionConfig(strategy="hash", json_paths=("$.customer.name",)).redactor().value(value)
    assert hashed["customer"]["name"].startswith("[HASH:field:")
    again, _ = RedactionConfig(strategy="hash", json_paths=("$.customer.name",)).redactor().value(value)
    assert again["customer"]["name"] == hashed["customer"]["name"], "hash strategy must be stable"


@pytest.mark.parametrize("path", ["customer.email", "$", "$.a[b]", "$..a"])
def test_invalid_json_paths_are_rejected(path: str) -> None:
    with pytest.raises(ValueError):
        RedactionConfig(json_paths=(path,)).redactor()


def test_custom_pattern() -> None:
    red = RedactionConfig(custom_patterns=(r"ACME-\d{6}",)).redactor()
    assert red.text("ticket ACME-123456 opened")[0] == "ticket [REDACTED:custom] opened"


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=200), st.integers(min_value=0, max_value=100))
def test_truncate_keeps_valid_utf8(s: str, limit: int) -> None:
    out, cut = truncate(s, limit)
    encoded = out.encode("utf-8")
    if cut:
        assert out.endswith("…[truncated]")
        assert len(encoded) <= max(limit, len("…[truncated]".encode()))
    else:
        assert out == s
