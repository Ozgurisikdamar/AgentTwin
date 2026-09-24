from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agenttwin_core.yamlsafe import YAMLDocumentError, dump_yaml, load_yaml


def test_core_schema_scalars() -> None:
    doc = load_yaml(
        "a: no\nb: yes\nc: on\nd: true\ne: False\nf: 12\ng: -3.5\nh: 1e3\ni: 2026-01-01\nj: ~\nk: null\n"
        "l: 0x1F\nm: 1:30\n"
    )
    assert doc == {
        "a": "no",
        "b": "yes",
        "c": "on",
        "d": True,
        "e": False,
        "f": 12,
        "g": -3.5,
        "h": 1000.0,
        "i": "2026-01-01",
        "j": None,
        "k": None,
        "l": "0x1F",
        "m": "1:30",
    }


def test_json_is_yaml() -> None:
    assert load_yaml('{"a": [1, 2, {"b": null}], "c": "d"}') == {"a": [1, 2, {"b": None}], "c": "d"}


@pytest.mark.parametrize(
    ("doc", "message"),
    [
        ("a: &x [1, 2]\nb: *x\n", "anchors are not allowed"),
        ("b: *x\n", "aliases are not allowed"),
        ("a: 1\na: 2\n", "duplicate key 'a'"),
        ("!!python/object/apply:os.system ['id']\n", "is not allowed"),
        ("a: !secret value\n", "is not allowed"),
        ("a: 1\n---\nb: 2\n", "only one YAML document"),
        ("1: one\n", "mapping keys must be strings"),
        ("a: .inf\n", ""),
        ("a: [unclosed\n", "invalid YAML"),
    ],
)
def test_rejections(doc: str, message: str) -> None:
    if doc == "a: .inf\n":
        # .inf is not a number under the core schema: it stays a string.
        assert load_yaml(doc) == {"a": ".inf"}
        return
    with pytest.raises(YAMLDocumentError, match=message):
        load_yaml(doc)


def test_errors_carry_positions() -> None:
    with pytest.raises(YAMLDocumentError) as exc:
        load_yaml("a: 1\nb:\n  c: 1\n  c: 2\n")
    assert exc.value.line == 4
    assert exc.value.column == 3


def test_limits() -> None:
    with pytest.raises(YAMLDocumentError, match="larger than"):
        load_yaml("a: " + "x" * 300_000)
    deep = "a:\n" + "".join("  " * i + "- \n" for i in range(1, 40))
    with pytest.raises(YAMLDocumentError, match="nested deeper"):
        load_yaml("[" * 40 + "]" * 40)
    with pytest.raises(YAMLDocumentError, match="more than 100 nodes"):
        load_yaml("[" + ",".join(["1"] * 200) + "]", max_nodes=100)
    del deep
    with pytest.raises(YAMLDocumentError, match="UTF-8"):
        load_yaml(b"a: \xff\xfe")


def test_billion_laughs_is_rejected_before_expansion() -> None:
    bomb = 'a: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]\n'
    for i in range(1, 9):
        bomb += f"{chr(97 + i)}: &{chr(97 + i)} [" + ",".join([f"*{chr(96 + i)}"] * 9) + "]\n"
    with pytest.raises(YAMLDocumentError, match="anchors are not allowed"):
        load_yaml(bomb)


scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**53), max_value=2**53),
    st.floats(allow_nan=False, allow_infinity=False, width=64),
    st.text(max_size=30),
)
json_like: st.SearchStrategy[Any] = st.recursive(
    scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=5), st.dictionaries(st.text(max_size=12), children, max_size=5)
    ),
    max_leaves=30,
)


@settings(max_examples=400, deadline=None)
@given(json_like)
def test_dump_then_load_round_trips(value: Any) -> None:
    doc = {"v": value}
    assert load_yaml(dump_yaml(doc)) == doc
