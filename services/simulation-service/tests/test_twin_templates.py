"""Twin templates: typed rendering, interpolation and injection-proof paths."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agenttwin_simulation.twin.templates import Env, TemplateError, check_template, render, render_path

ENV = Env(
    args={
        "order_id": "ORD-1",
        "amount": 12.5,
        "n": 2,
        "nested": {"k": [1, 2]},
        "flag": True,
        "id": "user-id",
    },
    state={"orders": {"ORD-1": {"total": 40}}},
    tenant="demo-co",
    now="2026-09-24T12:00:00Z",
    seq=7,
    call_number=3,
)


def test_single_placeholder_keeps_the_type() -> None:
    assert render("{amount}", ENV) == 12.5
    assert render("{nested}", ENV) == {"k": [1, 2]}
    assert render("{flag}", ENV) is True
    assert render("{state.orders.ORD-1.total}", ENV) == 40
    assert render("{call.number}", ENV) == 3


def test_interpolation_and_escapes() -> None:
    assert render("RF-{id}", ENV) == "RF-0007"  # reserved: the generated id, not the argument
    assert render("{args.id}", ENV) == "user-id"
    assert render("{order_id}:{amount}:{flag}", ENV) == "ORD-1:12.5:true"
    assert render("{{literal}} {n}", ENV) == "{literal} 2"
    assert render("{nested}!", ENV) == '{"k":[1,2]}!'
    assert render({"a": ["{n}", {"b": "{tenant}"}], "c": 1}, ENV) == {"a": [2, {"b": "demo-co"}], "c": 1}


def test_optional_placeholders() -> None:
    assert render("{missing?}", ENV) is None
    assert render("x{missing?}y", ENV) == "xy"
    with pytest.raises(TemplateError) as err:
        render("{missing}", ENV)
    assert err.value.argument == "missing" and err.value.missing


def test_rendered_values_are_copies() -> None:
    out = render("{nested}", ENV)
    out["k"].append(3)
    assert ENV.args["nested"] == {"k": [1, 2]}


@pytest.mark.parametrize(
    ("template", "segments"),
    [
        ("orders.{order_id}", ("orders", "ORD-1")),
        ("orders.{order_id}.total", ("orders", "ORD-1", "total")),
        ("items[{n}]", ("items", 2)),
        ("items[{order_id}]", ("items", "ORD-1")),
        ("orders.pre-{order_id}-post", ("orders", "pre-ORD-1-post")),
        ("$.a['b c']", ("a", "b c")),
    ],
)
def test_render_path(template: str, segments: tuple[object, ...]) -> None:
    assert render_path(template, ENV) == segments


@given(st.text(min_size=1, max_size=40))
def test_path_values_never_change_the_structure(value: str) -> None:
    env = Env(args={"x": value})
    assert render_path("a.{x}.b", env) == ("a", value, "b")


def test_path_errors_name_the_argument() -> None:
    with pytest.raises(TemplateError) as err:
        render_path("orders.{nested}", ENV)
    assert err.value.argument == "nested" and not err.value.missing
    with pytest.raises(TemplateError) as err:
        render_path("orders.{absent}", ENV)
    assert err.value.missing
    with pytest.raises(TemplateError, match="control characters"):
        render_path("a.\x00b", ENV)


@pytest.mark.parametrize(
    "template",
    ["{", "a}", "{}", "{1abc}", "{a b}", "{state.[x}"],
)
def test_check_template_reports_syntax_errors(template: str) -> None:
    assert check_template(template) is not None


def test_check_template_accepts_valid_templates() -> None:
    for t in ("plain", "{a}", "{args.a.b[0]}", "x-{id}-{call.seq}", "{opt?}", {"k": ["{v}"]}):
        assert check_template(t) is None
    assert check_template("orders.{id}.x", path=True) is None
    assert check_template("orders..x", path=True) is not None
