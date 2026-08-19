"""Shared test isolation.

Configuration resolves as ``defaults < dfs.toml < command-line flags``, and
``dfs.toml`` is discovered in the *current working directory*. Pytest runs
from the repository root, so once a developer creates their own ``dfs.toml``
-- which README.md tells them to do -- their personal settings leak into the
suite and tests asserting "this value came from a default" start failing.

That is a test-isolation defect, not a configuration bug: the resolution order
is correct, but a test must not depend on what happens to sit in the directory
it was launched from. Running every test in its own empty directory removes
the ambient file from consideration entirely.

Discovered 2026-08-19, when creating a real dfs.toml turned
test_run_directory_records_config_and_its_origins red. The suite had been
green only because no dfs.toml existed yet.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    """Run each test from an empty directory it owns.

    Every fixture path in this suite is absolute (anchored on ``__file__``),
    so nothing depends on the working directory being the repository root.
    """
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return workdir
