"""Tests for the operator runbook generator.

The runbook is a printed artifact, which means nobody finds out it is wrong
until they are standing at a terminal on a Sunday with a lock time coming.
Two things are worth pinning:

1. It still fits on one page. A runbook that silently spills onto a second
   page loses whatever fell off the bottom.
2. Every flag it names still exists. A flag rename is the realistic way this
   document goes stale, and it is invisible from inside the document.

RUNBOOK.pdf is committed rather than gitignored, so that anyone browsing the
repository can see the artifact without building it. That choice reintroduces
the risk gitignoring it avoided -- a printed document drifting away from the
code that produced it -- so a third test rebuilds and compares.

The generator needs ``reportlab``, which lives in the optional ``docs`` extra
rather than ``dev`` -- a PDF toolchain is not worth making everyone install to
run the suite. These tests skip cleanly when it is absent.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from dfs_pipeline.cli.optimize import build_parser as build_optimize_parser
from dfs_pipeline.cli.snapshot import build_parser as build_snapshot_parser

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATOR = REPO_ROOT / "tools" / "make_runbook.py"
COMMITTED = REPO_ROOT / "RUNBOOK.pdf"

pytest.importorskip("reportlab", reason="runbook generator needs the 'docs' extra")
pytest.importorskip("pypdf", reason="runbook generator needs the 'docs' extra")


def _load_generator():
    """Import tools/make_runbook.py by path; tools/ is not an importable package."""
    spec = importlib.util.spec_from_file_location("make_runbook", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generator_exists():
    """The PDF is a build output; this file is the thing that survives."""
    assert GENERATOR.is_file(), f"missing {GENERATOR}"


def test_runbook_builds_to_exactly_one_page(tmp_path):
    module = _load_generator()
    out = module.build_runbook(tmp_path / "RUNBOOK.pdf")

    from pypdf import PdfReader

    assert out.is_file()
    assert len(PdfReader(str(out)).pages) == 1


def _text(pdf: Path) -> str:
    """Extracted text of a PDF, used as the stable identity of its content."""
    from pypdf import PdfReader

    return "\n".join(page.extract_text() for page in PdfReader(str(pdf)).pages)


def test_committed_runbook_is_not_stale(tmp_path):
    """The checked-in PDF still matches the generator that produces it.

    This is the test that makes committing a build output defensible. Without
    it, editing tools/make_runbook.py and forgetting to rebuild ships a
    document that confidently describes a tool that has moved on -- and there
    is no CI here to notice.

    Compared on extracted text rather than bytes: reportlab stamps a creation
    date and a document id, so two builds of identical content are never
    byte-identical.
    """
    assert COMMITTED.is_file(), (
        f"{COMMITTED.name} is committed and should be present. "
        "Rebuild it: uv run python tools/make_runbook.py"
    )

    module = _load_generator()
    fresh = module.build_runbook(tmp_path / "fresh.pdf")

    assert _text(fresh) == _text(COMMITTED), (
        "RUNBOOK.pdf is out of date with tools/make_runbook.py. "
        "Rebuild it: uv run python tools/make_runbook.py"
    )


def test_runbook_does_not_write_outside_its_target(tmp_path):
    """--out is honoured, so a build never clobbers a checked-out RUNBOOK.pdf."""
    module = _load_generator()
    target = tmp_path / "nested" / "custom.pdf"
    out = module.build_runbook(target)
    assert out == target
    assert target.is_file()


def test_every_flag_named_in_the_runbook_still_exists():
    """The runbook cannot name a flag the CLIs no longer accept.

    Flags are read from the built document rather than from this file's
    source or from extracted PDF text. Source scanning picks up flags named
    in docstrings; PDF extraction breaks tokens across line wraps. Both fail
    for reasons that have nothing to do with drift.
    """
    module = _load_generator()
    named = set(re.findall(r"--[a-z][a-z0-9-]*", module.document_text()))

    real: set[str] = set()
    for parser in (build_snapshot_parser(), build_optimize_parser()):
        for action in parser._actions:
            real.update(action.option_strings)

    unknown = sorted(named - real)
    assert not unknown, (
        f"the runbook names flags that no longer exist: {unknown}. "
        f"Update tools/make_runbook.py and rebuild RUNBOOK.pdf."
    )


def test_runbook_documents_the_short_verbose_flag():
    """-v appears in the extras table; make sure it is a real alias."""
    module = _load_generator()
    documented = {label for label, _ in module.EXTRAS}
    assert "-v" in documented

    real: set[str] = set()
    for parser in (build_snapshot_parser(), build_optimize_parser()):
        for action in parser._actions:
            real.update(action.option_strings)
    assert "-v" in real
