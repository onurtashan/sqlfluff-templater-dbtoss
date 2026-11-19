"""sqlfluff templater that reads dbt-oss compiled output instead of compiling with dbt-core."""

import os
from typing import Any, Iterator, Optional

from sqlfluff.core.config import FluffConfig
from sqlfluff.core.errors import SQLTemplaterError
from sqlfluff.core.formatter import FormatterInterface
from sqlfluff.core.templaters.base import RawTemplater, TemplatedFile, large_file_check

import yaml

from .slicer import SliceError, build_templated_file


def _project_name(project_dir: str) -> str:
    path = os.path.join(project_dir, "dbt_project.yml")
    try:
        with open(path, encoding="utf-8") as fh:
            return str(yaml.safe_load(fh)["name"])
    except (OSError, KeyError, TypeError) as err:
        raise SQLTemplaterError(f"dbtoss: cannot read the project name from {path}; set compiled_dirs") from err


class DbtOssTemplater(RawTemplater):
    """Lint dbt models against the SQL that `dbt compile` (dbt-oss) already wrote.

    Config, under `[sqlfluff:templater:dbtoss]`:
      project_dir    dbt project root (default ".")
      compiled_dirs  comma-separated compiled roots, relative to project_dir unless absolute.
                     Default: "target/compiled/<name from dbt_project.yml>".
                     The first one is the variant that `fix` uses; the others are linted too
                     (e.g. the other `is_incremental()` branch).
    """

    name = "dbtoss"

    def _settings(self, config: Optional[FluffConfig]) -> tuple[str, list[str]]:
        section: dict[str, Any] = {}
        if config:
            section = config.get_section((self.templater_selector, self.name)) or {}
        project_dir = str(section.get("project_dir", "."))
        raw_dirs = section.get("compiled_dirs")
        if raw_dirs is None:
            raw_dirs = f"target/compiled/{_project_name(project_dir)}"
        dirs = [d.strip() for d in str(raw_dirs).split(",") if d.strip()]
        return project_dir, [d if os.path.isabs(d) else os.path.join(project_dir, d) for d in dirs]

    @large_file_check
    def process_with_variants(
        self,
        *,
        in_str: str,
        fname: str,
        config: Optional[FluffConfig] = None,
        formatter: Optional[FormatterInterface] = None,
    ) -> Iterator[tuple[TemplatedFile, list[SQLTemplaterError]]]:
        project_dir, compiled_dirs = self._settings(config)
        rel = os.path.relpath(os.path.abspath(fname), os.path.abspath(project_dir))
        seen: set[str] = set()
        found = False
        for root in compiled_dirs:
            path = os.path.join(root, rel)
            if not os.path.exists(path):
                continue
            found = True
            with open(path, encoding="utf-8") as fh:
                compiled = fh.read()
            try:
                tf = build_templated_file(in_str, compiled, fname)
            except SliceError as err:
                raise SQLTemplaterError(f"dbtoss: cannot align {path} with {fname}: {err}") from err
            if tf.templated_str in seen:
                continue
            seen.add(tf.templated_str)
            yield tf, []
        if not found:
            raise SQLTemplaterError(
                f"dbtoss: no compiled SQL for {rel} under {compiled_dirs}. Run `dbt compile` (dbt-oss) first."
            )

    def process(
        self,
        *,
        in_str: str,
        fname: str,
        config: Optional[FluffConfig] = None,
        formatter: Optional[FormatterInterface] = None,
    ) -> tuple[TemplatedFile, list[SQLTemplaterError]]:
        return next(self.process_with_variants(in_str=in_str, fname=fname, config=config, formatter=formatter))

    def config_pairs(self) -> list[tuple[str, str]]:
        return [("templater", self.name)]
