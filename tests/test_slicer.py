import pytest
from jinja2 import Environment

from sqlfluff_templater_dbtoss.slicer import SliceError, build_templated_file

# dbt renders models with keep_trailing_newline=False; use real Jinja so expected output is not hand-written.
ENV = Environment(extensions=["jinja2.ext.do", "jinja2.ext.loopcontrols"], keep_trailing_newline=False)


def render(src, incremental=False, **ctx):
    ctx.setdefault("ref", lambda name: f"db.schema.{name}")
    ctx.setdefault("this", "db.schema.m")
    ctx.setdefault("is_incremental", lambda: incremental)
    return ENV.from_string(src).render(**ctx)


def check(src, compiled):
    tf = build_templated_file(src, compiled, "m.sql")
    for s in tf.sliced_file:
        if s.slice_type == "literal" and s.templated_slice.stop > s.templated_slice.start:
            assert tf.source_str[s.source_slice] == tf.templated_str[s.templated_slice]
    return tf


def rendered_literals(tf):
    return [
        tf.source_str[s.source_slice]
        for s in tf.sliced_file
        if s.slice_type == "literal" and s.templated_slice.stop > s.templated_slice.start
    ]


def test_ref_expansion():
    src = "SELECT a\nFROM {{ ref('x') }}\n"
    tf = check(src, render(src))
    assert tf.templated_str == "SELECT a\nFROM db.schema.x\n"


@pytest.mark.parametrize("incremental", [True, False])
def test_if_branch(incremental):
    src = "SELECT a\nFROM t\n{% if is_incremental() %}\nWHERE b > (SELECT MAX(b) FROM {{ this }})\n{% endif %}\n"
    tf = check(src, render(src, incremental))
    assert ("\nWHERE b > (SELECT MAX(b) FROM " in rendered_literals(tf)) is incremental


@pytest.mark.parametrize("x", [True, False])
def test_else_branch(x):
    src = "SELECT\n{% if x %}\n    1 AS a\n{% else %}\n    2 AS a\n{% endif %}\nFROM t\n"
    tf = check(src, render(src, x=x))
    assert ("\n    1 AS a\n" in rendered_literals(tf)) is x
    assert ("\n    2 AS a\n" in rendered_literals(tf)) is not x


def test_for_loop_with_loop_last():
    src = (
        "SELECT\n"
        "{% for c in ['a', 'b', 'c'] %}\n"
        "    {{ c }}{% if not loop.last %},{% endif %}\n"
        "{% endfor %}\n"
        "FROM t\n"
    )
    tf = check(src, render(src))
    endfors = [s for s in tf.sliced_file if s.slice_type == "block_end" and "endfor" in tf.source_str[s.source_slice]]
    assert len(endfors) == 3


def test_empty_for_loop():
    src = "SELECT 1\n{% for c in [] %}\n, {{ c }}\n{% endfor %}\nFROM t\n"
    check(src, render(src))


def test_set_block_and_inline_set_and_comment():
    src = "{% set cols %}a, b{% endset %}\n{% set n = 1 %}\n{# a comment #}\nSELECT {{ cols }}, {{ n }}\nFROM t\n"
    check(src, render(src))


def test_whitespace_control():
    src = "SELECT a\n{%- if x %}\n    , b\n{%- endif %}\nFROM t\n"
    for x in (True, False):
        check(src, render(src, x=x))


def test_config_block_and_trailing_newlines():
    src = "{{ config(materialized='table') }}\n\nSELECT 1\n\n"
    tf = check(src, render(src, config=lambda **_: ""))
    assert tf.templated_str.endswith("SELECT 1\n\n")


def test_mismatch_raises():
    with pytest.raises(SliceError):
        build_templated_file("SELECT a\nFROM t\n", "SELECT b\nFROM t", "m.sql")
