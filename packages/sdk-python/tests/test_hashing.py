from __future__ import annotations

import json
import math
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agenttwin.hashing import canonical_json, content_hash
from sdk_testutil import json_values, load_fixture

FIXTURE = load_fixture("canonical-json.json")


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["name"])
def test_matches_shared_fixture(case: dict[str, Any]) -> None:
    assert canonical_json(case["input"]) == case["canonical"]


def test_hash_is_independent_of_key_order() -> None:
    a = {"x": 1, "y": {"b": 2, "a": 1}}
    b = {"y": {"a": 1, "b": 2}, "x": 1}
    assert content_hash(a) == content_hash(b)
    assert content_hash(a) != content_hash({"x": 2})
    assert len(content_hash(a)) == 64


def test_rejects_non_finite_and_non_string_keys() -> None:
    with pytest.raises(ValueError):
        canonical_json({"x": math.nan})
    with pytest.raises(TypeError):
        canonical_json({1: "x"})
    with pytest.raises(TypeError):
        canonical_json({"x": object()})


@settings(max_examples=300, deadline=None)
@given(json_values())
def test_canonical_form_is_a_fixed_point(value: Any) -> None:
    once = canonical_json(value)
    assert canonical_json(json.loads(once)) == once


@settings(max_examples=200, deadline=None)
@given(st.dictionaries(st.text(max_size=8), st.integers(), max_size=8))
def test_key_order_never_matters(d: dict[str, int]) -> None:
    reordered = dict(reversed(list(d.items())))
    assert canonical_json(d) == canonical_json(reordered)
