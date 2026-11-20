import os

from jinja2 import Environment
from sqlfluff.core import FluffConfig, Linter

ENV = Environment(keep_trailing_newline=False)
MODEL = """SELECT
    a
    , b
FROM {{ ref('x') }}
{% if is_incremental() %}
WHERE b > (select MAX(b) FROM {{ this }})
{% endif %}
"""


def render(src, incremental):
    return ENV.from_string(src).render(
        ref=lambda n: f"db.schema.{n}", this="db.schema.m", is_incremental=lambda: incremental
    )


def make_project(tmp_path, variants, model=MODEL):
    """variants: list of is_incremental() values, one compiled dir per value."""
    (tmp_path / "dbt_project.yml").write_text("name: demo\nversion: '1.0'\n")
    (tmp_path / "models").mkdir(exist_ok=True)
    (tmp_path / "models" / "m.sql").write_text(model)
    dirs = []
    for i, incremental in enumerate(variants):
        d = tmp_path / f"target-{i}" / "compiled" / "demo" / "models"
        d.mkdir(parents=True, exist_ok=True)
        (d / "m.sql").write_text(render(model, incremental))
        dirs.append(f"target-{i}/compiled/demo")
    return dirs


def lint(tmp_path, dirs=None, fix=False):
    section = f"[sqlfluff:templater:dbtoss]\ncompiled_dirs = {','.join(dirs)}\n" if dirs is not None else ""
    (tmp_path / ".sqlfluff").write_text("[sqlfluff]\ndialect = snowflake\ntemplater = dbtoss\nrules = CP01\n\n" + section)
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        return Linter(config=FluffConfig.from_root()).lint_paths(("models/m.sql",), fix=fix, apply_fixes=fix)
    finally:
        os.chdir(old)


def violations(result):
    return [(v.rule_code(), v.line_no) for v in result.get_violations()]


def test_full_refresh_variant_cannot_see_incremental_branch(tmp_path):
    dirs = make_project(tmp_path, [False])
    assert violations(lint(tmp_path, dirs)) == []


def test_two_variants_lint_both_branches_with_source_lines(tmp_path):
    dirs = make_project(tmp_path, [False, True])
    assert violations(lint(tmp_path, dirs)) == [("CP01", 6)]


def test_default_compiled_dir_comes_from_dbt_project_name(tmp_path):
    make_project(tmp_path, [True])
    (tmp_path / "target-0").rename(tmp_path / "target")
    assert violations(lint(tmp_path)) == [("CP01", 6)]


def test_noqa_in_source_is_respected(tmp_path):
    model = MODEL.replace("FROM {{ this }})", "FROM {{ this }})  -- noqa: CP01")
    dirs = make_project(tmp_path, [False, True], model=model)
    assert violations(lint(tmp_path, dirs)) == []


def test_fix_writes_source_not_compiled(tmp_path):
    dirs = make_project(tmp_path, [True])
    lint(tmp_path, dirs, fix=True)
    fixed = (tmp_path / "models" / "m.sql").read_text()
    assert fixed == MODEL.replace("(select MAX(b)", "(SELECT MAX(b)")


def test_missing_compiled_file_is_a_templating_error(tmp_path):
    make_project(tmp_path, [])
    res = lint(tmp_path, ["target-x/compiled/demo"])
    assert [v.rule_code() for v in res.get_violations()] == ["TMP"]


def test_fix_applies_fixes_from_every_variant(tmp_path):
    dirs = make_project(tmp_path, [False, True])
    lint(tmp_path, dirs, fix=True)
    assert (tmp_path / "models" / "m.sql").read_text() == MODEL.replace("(select MAX(b)", "(SELECT MAX(b)")


def test_snapshot_compiled_into_a_directory_is_found(tmp_path):
    # dbt-oss writes a snapshot to <name>.sql/<name>.sql, not to <name>.sql.
    (tmp_path / "dbt_project.yml").write_text("name: demo\nversion: '1.0'\n")
    (tmp_path / "snapshots").mkdir()
    (tmp_path / "snapshots" / "s.sql").write_text("SELECT a from t\n")
    d = tmp_path / "target" / "compiled" / "demo" / "snapshots" / "s.sql"
    d.mkdir(parents=True)
    (d / "s.sql").write_text("SELECT a from t")
    (tmp_path / ".sqlfluff").write_text("[sqlfluff]\ndialect = snowflake\ntemplater = dbtoss\nrules = CP01\n")
    os.chdir(tmp_path)
    result = Linter(config=FluffConfig.from_root()).lint_paths(["snapshots/s.sql"])
    assert [v.rule_code() for v in result.get_violations()] == ["CP01"]


def test_macro_files_are_skipped(tmp_path):
    (tmp_path / "dbt_project.yml").write_text("name: demo\nversion: '1.0'\n")
    (tmp_path / "macros").mkdir()
    (tmp_path / "macros" / "m.sql").write_text("{% macro m() %}select 1{% endmacro %}\n")
    (tmp_path / ".sqlfluff").write_text("[sqlfluff]\ndialect = snowflake\ntemplater = dbtoss\n")
    os.chdir(tmp_path)
    result = Linter(config=FluffConfig.from_root()).lint_paths(["macros/m.sql"])
    assert result.get_violations() == []


def test_generic_test_definitions_are_skipped(tmp_path):
    (tmp_path / "dbt_project.yml").write_text("name: demo\nversion: '1.0'\n")
    (tmp_path / "tests" / "generic").mkdir(parents=True)
    (tmp_path / "tests" / "generic" / "t.sql").write_text("{% test t(model) %}select 1{% endtest %}\n")
    (tmp_path / ".sqlfluff").write_text("[sqlfluff]\ndialect = snowflake\ntemplater = dbtoss\n")
    os.chdir(tmp_path)
    result = Linter(config=FluffConfig.from_root()).lint_paths(["tests/generic/t.sql"])
    assert result.get_violations() == []
