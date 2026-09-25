from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agenttwin_core.compare import RegexTimeout, compare, search
from agenttwin_core.paths import (
    LENGTH,
    MISSING,
    PathError,
    delete_path,
    format_path,
    get_path,
    json_equal,
    parse_path,
    set_path,
)


@pytest.mark.parametrize(
    ("path", "segments"),
    [
        ("orders.ORD-1001.status", ("orders", "ORD-1001", "status")),
        ("$.items[0].id", ("items", 0, "id")),
        ("$", ()),
        ("", ()),
        ("refunds[-1]", ("refunds", -1)),
        ("a['x.y'][\"q\"]", ("a", "x.y", "q")),
        ("a['it\\'s']", ("a", "it's")),
        ("refunds.length()", ("refunds", LENGTH)),
        ("items.length().x", ("items", LENGTH, "x")),
        ("[2]", (2,)),
    ],
)
def test_parse(path: str, segments: tuple[Any, ...]) -> None:
    assert parse_path(path) == segments


@pytest.mark.parametrize("path", ["a..b", "a[", "a[x]", "a['x", "a b", ".", "a[0", "$x"])
def test_parse_errors(path: str) -> None:
    with pytest.raises(PathError):
        parse_path(path)


def test_path_limits() -> None:
    with pytest.raises(PathError, match="longer"):
        parse_path("a" * 501)
    with pytest.raises(PathError, match="segments"):
        parse_path(".".join(["a"] * 65))


def test_get_set_delete() -> None:
    data: dict[str, Any] = {"orders": {"ORD-1": {"status": "delivered", "tags": ["a", "b"]}}}
    assert get_path(data, "orders.ORD-1.status") == "delivered"
    assert get_path(data, "orders.ORD-1.tags[-1]") == "b"
    assert get_path(data, "orders.ORD-1.tags.length()") == 2
    assert get_path(data, "orders.ORD-2.status") is MISSING
    assert get_path(data, "orders.ORD-1.tags[5]") is MISSING
    assert get_path(data, "orders.ORD-1.status.x") is MISSING
    set_path(data, "orders.ORD-2.status", "shipped")
    assert data["orders"]["ORD-2"] == {"status": "shipped"}
    set_path(data, "orders.ORD-1.tags[0]", "z")
    assert data["orders"]["ORD-1"]["tags"] == ["z", "b"]
    with pytest.raises(PathError):
        set_path(data, "orders.ORD-1.tags[9]", "x")
    with pytest.raises(PathError):
        set_path(data, "orders.ORD-1.status.deeper", 1)
    assert delete_path(data, "orders.ORD-2") is True
    assert delete_path(data, "orders.ORD-2") is False
    assert delete_path(data, "orders.ORD-1.tags[0]") is True
    assert data["orders"]["ORD-1"]["tags"] == ["b"]


def test_json_equal_semantics() -> None:
    assert json_equal(1, 1.0)
    assert json_equal(0.1 + 0.2, 0.3)
    assert not json_equal(True, 1)
    assert not json_equal(0, False)
    assert json_equal({"a": [1, {"b": None}]}, {"a": [1.0, {"b": None}]})
    assert not json_equal({"a": 1}, {"a": 1, "b": 2})
    assert not json_equal("1", 1)
    assert not json_equal([1, 2], [2, 1])


def test_comparators() -> None:
    assert compare(5, {"gte": 5, "lt": 6}).ok
    assert not compare(5, {"gt": 5}).ok
    assert compare("refunded", {"in": ["refunded", "closed"]}).ok
    assert not compare(MISSING, {"equals": None}).ok
    assert compare(MISSING, {"exists": False}).ok
    assert compare(None, {"exists": False}).ok
    assert not compare(0, {"exists": False}).ok
    assert compare("abc", {}).ok
    assert not compare(MISSING, {}).ok
    assert compare("ORD-1001", {"matches": r"^ORD-\d+$", "notEquals": "ORD-9"}).ok
    r = compare("x", {"gte": 1})
    assert not r.ok and "numbers" in r.reason
    r = compare(True, {"lte": 3})
    assert not r.ok  # booleans are not numbers


def test_regex_timeout_is_reported() -> None:
    with pytest.raises(RegexTimeout):
        search(r"(a+)+$", "a" * 40_000 + "!")


@given(st.dictionaries(st.from_regex(r"[a-z][a-z0-9_-]{0,8}", fullmatch=True), st.integers(), max_size=5))
def test_set_then_get_round_trips(values: dict[str, int]) -> None:
    data: dict[str, Any] = {}
    for key, v in values.items():
        set_path(data, f"root.{key}.value", v)
    for key, v in values.items():
        assert get_path(data, f"root.{key}.value") == v


_segment = st.one_of(
    st.integers(min_value=-5, max_value=50),
    st.text(min_size=1, max_size=12),
)


@given(st.lists(_segment, min_size=1, max_size=8))
def test_format_path_round_trips(segments: list[Any]) -> None:
    assert parse_path(format_path(segments)) == tuple(segments)


def test_format_path_quotes_what_needs_quoting() -> None:
    assert format_path(["orders", "ORD-1", "status"]) == "orders.ORD-1.status"
    assert format_path(["orders", "a.b", "it's", 0]) == "orders['a.b']['it\\'s'][0]"
    assert format_path(["$x", "length()"]) == "['$x']['length()']"
    assert format_path(["items", LENGTH]) == "items.length()"


def test_setters_accept_segments() -> None:
    data: dict[str, Any] = {}
    set_path(data, ("orders", "a.b", "status"), "paid")
    assert data == {"orders": {"a.b": {"status": "paid"}}}
    assert delete_path(data, ("orders", "a.b", "status"))
    assert data == {"orders": {"a.b": {}}}
