"""Tests that the package is actually installed and wired up correctly.

Because this project uses a src/ layout, these tests can only pass against an
installed package. If packaging breaks, they fail rather than silently
importing loose files from the working directory.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

import dfs_pipeline
from dfs_pipeline.cli.snapshot import EXIT_NOT_IMPLEMENTED, main


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
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == dfs_pipeline.__version__


def test_snapshot_stub_exits_nonzero():
    """The stub must not report success it has not earned.

    When capture is implemented this test gets replaced. Until then it pins
    the honest behaviour: the command exists, and it admits it does nothing.
    """
    assert main([]) == EXIT_NOT_IMPLEMENTED
    assert EXIT_NOT_IMPLEMENTED != 0


@pytest.mark.skipif(
    shutil.which("dfs-snapshot") is None,
    reason="console script not on PATH (package not installed in this env)",
)
def test_console_script_is_on_path_and_exits_nonzero():
    """End-to-end check that pyproject's entry point actually works."""
    result = subprocess.run(
        ["dfs-snapshot"], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == EXIT_NOT_IMPLEMENTED
    assert "not implemented" in result.stderr.lower()


def test_running_as_a_module_also_works():
    """`python -m dfs_pipeline.cli.snapshot` should behave identically."""
    result = subprocess.run(
        [sys.executable, "-m", "dfs_pipeline.cli.snapshot"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == EXIT_NOT_IMPLEMENTED
