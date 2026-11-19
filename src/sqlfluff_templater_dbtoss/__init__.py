"""sqlfluff plugin: lint dbt models from dbt-oss compiled output (no dbt-core needed)."""

from sqlfluff.core.plugin import hookimpl

from .templater import DbtOssTemplater


@hookimpl
def get_templaters():
    return [DbtOssTemplater]
