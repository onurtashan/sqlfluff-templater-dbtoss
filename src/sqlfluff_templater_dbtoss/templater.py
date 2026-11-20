"""sqlfluff templater that reads dbt-oss compiled output instead of compiling with dbt-core."""

import os
from typing import Any, Iterator, Optional

from sqlfluff.core.config import FluffConfig
from sqlfluff.core.errors import SQLFluffSkipFile, SQLTemplaterError
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


def _macro_paths(project_dir: str) -> list[str]:
    """Paths dbt reads as macros: macro-paths (default "macros") and <test-paths>/generic."""
    try:
        with open(os.path.join(project_dir, "dbt_project.yml"), encoding="utf-8") as fh:
            project = yaml.safe_load(fh) or {}
    except OSError:
        project = {}
    macro_paths = project.get("macro-paths") or ["macros"]
    test_paths = project.get("test-paths") or ["tests"]
    paths = [str(p) for p in macro_paths] + [os.path.join(str(p), "generic") for p in test_paths]
    return [os.path.normpath(p) for p in paths]


def _compiled_path(root: str, rel: str) -> Optional[str]:
    """Return the compiled file for a source file, or None.

    dbt-oss writes models, tests and analyses to ``<name>.sql``, but a snapshot to the
    directory ``<name>.sql/`` with the SQL in ``<name>.sql/<name>.sql``.
    """
    path = os.path.join(root, rel)
    if os.path.isdir(path):
        path = os.path.join(path, os.path.basename(rel))
    return path if os.path.isfile(path) else None


class DbtOssTemplater(RawTemplater):
    """Lint dbt models against the SQL that `dbt compile` (dbt-oss) already wrote.

    Config, under `[sqlfluff:templater:dbtoss]`:
      project_dir    dbt project root (default ".")
      compiled_dirs  comma-separated compiled roots, relative to project_dir unless absolute.
                     Default: "target/compiled/<name from dbt_project.yml>".
                     Every dir is a variant (e.g. one per `is_incremental()` branch). SQLFluff lints
                     all variants and applies fixes from all of them to the model source.
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
        # Macro files are not compiled on their own. Skip them, like the dbt templater does.
        if any(rel == p or rel.startswith(p + os.sep) for p in _macro_paths(project_dir)):
            raise SQLFluffSkipFile(f"Skipped file {fname} because it is a macro")
        seen: set[str] = set()
        found = False
        for root in compiled_dirs:
            path = _compiled_path(root, rel)
            if path is None:
                continue
            found = True
            try:
                with open(path, encoding="utf-8") as fh:
                    compiled = fh.read()
            except OSError as err:
                raise SQLTemplaterError(f"dbtoss: cannot read {path}: {err}") from err
            try:
                tf = build_templated_file(in_str, compiled, fname)
            except SliceError as err:
                raise SQLTemplaterError(f"dbtoss: cannot align {path} with {fname}: {err}") from err
            except Exception as err:  # noqa: BLE001 - report one file, do not crash the whole lint run
                raise SQLTemplaterError(
                    f"dbtoss: internal error on {fname} ({type(err).__name__}: {err}). Please report it."
                ) from err
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
