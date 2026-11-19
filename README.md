# sqlfluff-templater-dbtoss

A [SQLFluff](https://sqlfluff.com) templater that lints dbt models from **dbt-oss** (dbt v2, Apache-2.0) compiled output. It does not need dbt-core.

## Why

On dbt v2, the usual setup stops working:

- `sqlfluff-templater-dbt` imports dbt-core. dbt-core and dbt-oss install into the same `dbt` Python package, so they cannot share one environment ([sqlfluff#8505](https://github.com/sqlfluff/sqlfluff/issues/8505)).
- The native `dbt lint` command ships only in the proprietary `dbt` (Fusion) distribution, not in dbt-oss.

This templater reads the SQL that `dbt compile` already wrote. It gives SQLFluff a full template map, so linting works like it does with the dbt templater:

- Violations point to lines in your model file, not the compiled file.
- `-- noqa` comments in the model work.
- `sqlfluff fix` edits the model file and leaves Jinja untouched.
- Rules that skip templated code (layout, capitalisation, aliasing) treat `{{ ... }}` output as templated.

## Install

Use a separate environment from dbt-oss:

```bash
pip install sqlfluff-templater-dbtoss
```

## Use

1. Compile with dbt-oss:

   ```bash
   dbt compile
   ```

2. Configure SQLFluff (`.sqlfluff` in the project root):

   ```ini
   [sqlfluff]
   dialect = snowflake
   templater = dbtoss

   [sqlfluff:templater:dbtoss]
   project_dir = .
   # optional; default: target/compiled/<name in dbt_project.yml>
   compiled_dirs = target/compiled/my_project
   ```

3. Lint or fix:

   ```bash
   sqlfluff lint models
   sqlfluff fix models
   ```

If a model has no compiled file, SQLFluff reports a `TMP` error for it. Run `dbt compile` again after you change a model.

## Lint every `is_incremental()` branch

One compile renders one branch of each `{% if %}`. To lint both branches of `is_incremental()`, compile twice and list both directories. SQLFluff lints every directory in `compiled_dirs`; `fix` uses the first one.

```bash
mkdir -p macros/_lint_overlay
echo "{% macro is_incremental() %}{{ return(false) }}{% endmacro %}" > macros/_lint_overlay/is_incremental.sql
dbt compile --target-path target-full
echo "{% macro is_incremental() %}{{ return(true) }}{% endmacro %}" > macros/_lint_overlay/is_incremental.sql
dbt compile --target-path target-incremental
rm -r macros/_lint_overlay
```

```ini
[sqlfluff:templater:dbtoss]
compiled_dirs = target-full/compiled/my_project, target-incremental/compiled/my_project
```

Do not use `--full-refresh` for the first compile. It has no effect on models with `full_refresh=false`.

## How it works

1. SQLFluff's own Jinja lexer (`JinjaAnalyzer`) splits the model into slices: literal SQL, `{{ }}`, `{% %}` tags, comments. This step does not render anything.
2. The slices become a pattern: literal SQL must match exactly, `{{ }}` matches any text, `if` blocks are alternatives, `for` blocks repeat, `set` blocks and comments match nothing.
3. The pattern is matched against the compiled SQL. The match shows which branches ran, how many times each loop ran and what each expression produced.
4. The result is a SQLFluff `TemplatedFile`, the same structure the Jinja and dbt templaters produce.

dbt renders with `keep_trailing_newline=False`, which drops one final newline. The templater adds it back, like `sqlfluff-templater-dbt` does.

## Limits

- It uses SQLFluff internals (`JinjaAnalyzer`). The package pins one SQLFluff minor version. Test before you widen the pin.
- Branches other than `is_incremental()` are linted only as far as one compile renders them. This is the same as the dbt templater.
- The compiled SQL must come from the same model source. Lint after `dbt compile`, not before.

## Development

```bash
pip install -e ".[test]"
pytest
```

Tests render the expected compiled SQL with real Jinja2, using the same `keep_trailing_newline=False` setting as dbt.

## License

Apache-2.0. See [LICENSE](LICENSE).

"dbt" is a trademark of dbt Labs, Inc. This project is not affiliated with or endorsed by dbt Labs.
