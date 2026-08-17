"""Tests that the package is actually installed and wired up correctly.

Because this project uses a src/ layout, these tests can only pass against an
installed package. If packaging breaks, they fail rather than silently
importing loose files from the working directory.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

import dfs_pipeline
from dfs_pipeline.cli.snapshot import EXIT_OK, EXIT_USAGE, build_parser


def _uninstrumented_env() -> dict[str, str]:
    """Environment with pytest-cov's subprocess hooks removed.

    These tests spawn children to prove the package is installed and the
    console script works -- packaging questions, not coverage ones. Left
    instrumented, a child launched with a different working directory cannot
    find pyproject.toml, silently falls back to statement-only coverage, and
    then fails to merge with the parent's branch data.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("COV_CORE_") and k != "COVERAGE_PROCESS_START"
    }


def test_package_imports_and_reports_a_version():
    assert dfs_pipeline.__version__ == "0.1.0"


def test_package_imports_from_an_unrelated_working_directory(tmp_path):
    """Proves the package is genuinely installed, not merely found via cwd.

    This is the property the src/ layout buys. With a top-level package,
    Python would find the code by looking in the current directory, so tests
    could pass against a package that was never installed. Importing
    successfully from an empty temporary directory can only work if the
    virtualenv actually knows about the package.

    Note that `dfs_pipeline.__file__` still points into src/ -- `uv sync`
    installs the project in editable mode, which is correct for development.
    Editability is not the same thing as being importable by accident.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import dfs_pipeline; print(dfs_pipeline.__version__)"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=30,
        env=_uninstrumented_env(),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == dfs_pipeline.__version__


def test_snapshot_command_is_wired_up():
    """The console script resolves to a parser that actually builds."""
    parser = build_parser()
    assert parser.prog == "dfs-snapshot"
    args = parser.parse_args(["--salaries", "x.csv"])
    assert args.salaries == "x.csv"


@pytest.mark.skipif(
    shutil.which("dfs-snapshot") is None,
    reason="console script not on PATH (package not installed in this env)",
)
def test_console_script_is_on_path():
    """End-to-end check that pyproject's entry point actually works."""
    result = subprocess.run(
        ["dfs-snapshot", "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        env=_uninstrumented_env(),
    )
    assert result.returncode == EXIT_OK
    assert "0.1.0" in result.stdout


def test_running_as_a_module_also_works():
    """`python -m dfs_pipeline.cli.snapshot` should behave identically."""
    result = subprocess.run(
        [sys.executable, "-m", "dfs_pipeline.cli.snapshot"],
        capture_output=True,
        text=True,
        timeout=30,
        env=_uninstrumented_env(),
    )
    assert result.returncode == EXIT_USAGE
    assert "nothing to capture" in result.stderr
