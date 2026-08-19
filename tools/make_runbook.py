#!/usr/bin/env python3
"""Build RUNBOOK.pdf -- the one-page operator card for running this pipeline.

The runbook exists because the tool has to be usable on a Sunday morning
without reconstructing how it works. That makes it exactly the kind of
document that rots: a flag gets renamed, the PDF still says the old name, and
the runbook is now worse than nothing because it is confidently wrong.

Keeping the generator in the repository is the fix. The document is rebuilt
from source rather than re-authored, and ``tests/test_runbook.py`` asserts
that every flag named in this file still exists in the live argument parsers.
A flag rename therefore breaks the test suite rather than silently breaking
the runbook.

Build it with the ``docs`` extra installed::

    uv sync --extra docs
    uv run python tools/make_runbook.py                 # -> ./RUNBOOK.pdf
    uv run python tools/make_runbook.py --out ~/Desktop/RUNBOOK.pdf
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "RUNBOOK.pdf"

INK = colors.HexColor("#111418")
MUTED = colors.HexColor("#5A6472")
RULE = colors.HexColor("#C8CFD8")
CODE_BG = colors.HexColor("#F2F4F7")
ACCENT = colors.HexColor("#134F97")

PAGE_W, PAGE_H = letter
MARGIN = 0.5 * inch
LABEL_COL = 1.62 * inch

title = ParagraphStyle(
    "title", fontName="Helvetica-Bold", fontSize=16, leading=18, textColor=INK,
)
subtitle = ParagraphStyle(
    "subtitle", fontName="Helvetica", fontSize=8.7, leading=11.0, textColor=MUTED,
    spaceBefore=2,
)
h = ParagraphStyle(
    "h", fontName="Helvetica-Bold", fontSize=10.2, leading=11.6, textColor=colors.white,
    backColor=ACCENT, borderPadding=(3, 5, 3, 5), spaceBefore=9, spaceAfter=4,
)
body = ParagraphStyle(
    "body", fontName="Helvetica", fontSize=9.0, leading=11.2, textColor=INK,
    alignment=TA_LEFT, spaceAfter=2.5,
)
note = ParagraphStyle(
    "note", parent=body, fontSize=8.2, leading=10.2, textColor=MUTED,
)
cell = ParagraphStyle("cell", parent=body, fontSize=8.2, leading=10.0, spaceAfter=0)
cellmono = ParagraphStyle(
    "cellmono", parent=cell, fontName="Courier-Bold",
    textColor=colors.HexColor("#8A2B18"),
)


def code(lines: list[str]) -> Table:
    """A shaded, monospaced command block.

    Leading spaces are converted to non-breaking spaces because Paragraph
    collapses runs of whitespace, which would flatten the indentation of a
    continued shell line and make the continuation look like a new command.
    """
    style = ParagraphStyle(
        "code", fontName="Courier", fontSize=8.5, leading=10.8, textColor=INK,
    )
    rows = []
    for ln in lines:
        esc = ln.replace("&", "&amp;").replace("<", "&lt;")
        indent = len(esc) - len(esc.lstrip(" "))
        rows.append([Paragraph("&nbsp;" * indent + esc.lstrip(" "), style)])

    t = Table(rows, colWidths=[PAGE_W - 2 * MARGIN])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CODE_BG),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
        ("TOPPADDING", (0, 0), (-1, 0), 4),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 4),
        ("LINEBEFORE", (0, 0), (0, -1), 2, ACCENT),
    ]))
    return t


def two_column(rows: list[tuple[str, str]]) -> Table:
    """A reference table whose left column is a literal string from the tool."""
    data = [
        [Paragraph(f"<b>{a}</b>", cellmono), Paragraph(b, cell)] for a, b in rows
    ]
    t = Table(data, colWidths=[LABEL_COL, PAGE_W - 2 * MARGIN - LABEL_COL])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (0, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 2.7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.7),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
    ]))
    return t


#: Left column is the text the tool actually prints; right column is what to
#: do about it. Keyed on printed output so the reader can match by eye.
FAILURES: list[tuple[str, str]] = [
    ("exit code 3",
     "Your data was rejected, not the tool. The message names the file, row, and "
     "column. Usually a vendor renamed a header."),
    ("DST: 0 in the pool",
     "The projections file has no defenses. Download the DST file too, or the "
     "optimizer cannot fill the DST slot."),
    ("already captured",
     "You are re-running a capture that already landed. Re-run with "
     "--on-duplicate ignore to skip the duplicates."),
    ("refusing to spend",
     "Odds credits are below the safety floor. Check with --quota (free). The "
     "free tier resets monthly; 2 credits per capture."),
    ("produced 11 of 20",
     "Constraints are too tight for the pool. The message names which one. "
     "Loosen --max-exposure or lower --stack."),
    ("cannot reach DraftKings",
     "The endpoint is an unofficial one and can change. Fall back to a manual "
     "download: --salaries ~/Downloads/DKSalaries.csv"),
    ("exit code 1",
     "Environment, not data. Usually a missing ODDS_API_KEY in .env, or the "
     "store path in dfs.toml pointing somewhere gone."),
]

EXTRAS: list[tuple[str, str]] = [
    ("data/snapshots.sqlite",
     "Every observation ever captured. Append-only &mdash; nothing is overwritten."),
    ("runs/",
     "One self-contained directory per run, written even when a run fails."),
    ("dfs.toml", "Your standing defaults. Command-line flags always win."),
    (".env", "ODDS_API_KEY lives here. Never committed."),
    ("--show-config",
     "Prints the settings actually in effect and where each one came from."),
    ("--quota", "Remaining Odds API credits. Costs nothing."),
    ("-v", "Add to any command for verbose progress output."),
]


def story() -> list:
    """The document, in order. Kept separate from the build so it can be read."""
    s: list = []

    s.append(Paragraph("NFL DFS Pipeline &mdash; Weekly Runbook", title))
    s.append(Paragraph(
        "Everything below runs from <font face='Courier'>~/Projects/nfl-dfs-pipeline</font>. "
        "Open Terminal, run <font face='Courier'>cd ~/Projects/nfl-dfs-pipeline</font> once, "
        "then use the commands as written. Nothing here logs into DraftKings or submits "
        "an entry &mdash; you always upload the finished file yourself.",
        subtitle,
    ))
    s.append(Spacer(1, 3))

    s.append(Paragraph(
        "1 &nbsp;&middot;&nbsp; CAPTURE THE SLATE &mdash; before lock, every week", h))
    s.append(Paragraph(
        "Download the projections CSV from Daily Fantasy Fuel or FantasyPros to "
        "<font face='Courier'>~/Downloads</font> first. Then capture salaries, betting "
        "odds, and projections in one command:",
        body,
    ))
    s.append(code([
        "uv run dfs-snapshot --slate-api --odds \\",
        "    --projections ~/Downloads/DFF_NFL_cheatsheet_2026-09-13.csv",
    ]))
    s.append(Paragraph(
        "<b>Read the match report it prints.</b> A rate near 100% is healthy. Any "
        "<b>WARNING</b> naming a player above $5,000 with no projection means a name "
        "failed to match &mdash; fix or accept it knowingly before optimizing. Add "
        "<font face='Courier'>--dry-run</font> to validate a file without writing "
        "anything. Capture whatever week you can: point-in-time odds and projections "
        "cannot be bought back later.",
        note,
    ))

    s.append(Paragraph("2 &nbsp;&middot;&nbsp; BUILD LINEUPS", h))
    s.append(code([
        "uv run dfs-optimize --slate-api \\",
        "    --projections ~/Downloads/DFF_NFL_cheatsheet_2026-09-13.csv \\",
        "    --lineups 20 --stack 2 --bringback 1 --max-exposure 0.4",
    ]))
    s.append(Paragraph(
        "The last line prints a run directory such as "
        "<font face='Courier'>runs/2026-09-13T18-04-22Z-optimize/</font>. Inside it, "
        "<b><font face='Courier'>lineups.csv</font></b> is already in DraftKings' "
        "bulk-upload format &mdash; go to the contest, choose <i>Import Lineups</i>, and "
        "upload that file. The same directory holds the match report, the post-solve "
        "validation report, and the SHA-256 of every input, so any lineup can be traced "
        "back to the exact data that produced it.",
        note,
    ))
    s.append(Paragraph(
        "<b>Useful flags:</b> "
        "<font face='Courier'>--stack N</font> pairs the QB with N teammates &middot; "
        "<font face='Courier'>--bringback N</font> adds N opponents from that same game "
        "&middot; <font face='Courier'>--max-exposure 0.4</font> caps any player at 40% "
        "of lineups &middot; <font face='Courier'>--lock \"Name\"</font> forces a player "
        "in &middot; <font face='Courier'>--ban \"Name\"</font> keeps one out. Drop "
        "<font face='Courier'>--stack</font> and <font face='Courier'>--bringback</font> "
        "for cash games.",
        note,
    ))

    s.append(Paragraph(
        "3 &nbsp;&middot;&nbsp; SCORE THE WEEK &mdash; Tuesday, after the games settle", h))
    s.append(code(["uv run dfs-snapshot --results --season 2026 --week 1"]))
    s.append(Paragraph(
        "Pulls final stats from nflverse and scores them at DraftKings Classic rules. "
        "nflverse posts on a delay &mdash; if a week comes back empty, wait a day and "
        "re-run.",
        note,
    ))

    s.append(Paragraph("WHEN SOMETHING GOES WRONG", h))
    s.append(two_column(FAILURES))

    s.append(Spacer(1, 5))
    s.append(Paragraph("WHERE THINGS LIVE &nbsp;&middot;&nbsp; HANDY EXTRAS", h))
    s.append(two_column(EXTRAS))

    return s


def document_text() -> str:
    """Every string that actually appears in the rendered document.

    Walking the built flowables is deliberate. Scanning this file's source
    would also pick up flags named in docstrings and comments, and extracting
    text back out of the PDF would break tokens across line wraps. Neither is
    the document. This is.
    """
    parts: list[str] = []

    def walk(node) -> None:
        if isinstance(node, Paragraph):
            parts.append(node.text)
        elif isinstance(node, Table):
            for row in getattr(node, "_cellvalues", []):
                walk(row)
        elif isinstance(node, (list, tuple)):
            for child in node:
                walk(child)

    walk(story())
    return "\n".join(parts)


def _decorate(canvas, doc) -> None:
    canvas.saveState()
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    y = MARGIN - 6
    canvas.line(MARGIN, y, PAGE_W - MARGIN, y)
    canvas.setFont("Helvetica", 6.6)
    canvas.setFillColor(MUTED)
    canvas.drawString(MARGIN, y - 9, "nfl-dfs-pipeline — operator runbook")
    canvas.drawRightString(
        PAGE_W - MARGIN, y - 9,
        "Rebuild with tools/make_runbook.py. "
        "No code in this project authenticates as you or submits an entry.",
    )
    canvas.restoreState()


class RunbookTooLong(RuntimeError):
    """Raised when the document no longer fits on a single page.

    A two-page "one-page runbook" is a silent failure: it still builds, still
    prints, and quietly loses whatever fell off the bottom. Failing the build
    is the only way that stays visible.
    """


def build_runbook(out: Path | str = DEFAULT_OUT) -> Path:
    """Render the runbook to ``out`` and return the path.

    Raises :class:`RunbookTooLong` if the result is not exactly one page.
    """
    out = Path(out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)

    doc = BaseDocTemplate(
        str(out), pagesize=letter,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN + 4,
        title="NFL DFS Pipeline - Weekly Runbook",
        author="Adam Wiggins",
    )
    frame = Frame(
        MARGIN, MARGIN + 4, PAGE_W - 2 * MARGIN, PAGE_H - 2 * MARGIN - 4,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
    )
    doc.addPageTemplates([PageTemplate(id="p", frames=[frame], onPage=_decorate)])
    doc.build(story())

    from pypdf import PdfReader

    pages = len(PdfReader(str(out)).pages)
    if pages != 1:
        raise RunbookTooLong(
            f"{out} came out {pages} pages; the runbook must fit on one. "
            "Trim content or reduce the type scale near the top of this file."
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="make_runbook",
        description="Build the one-page operator runbook (RUNBOOK.pdf).",
    )
    parser.add_argument(
        "--out", default=str(DEFAULT_OUT),
        help="where to write the PDF (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    try:
        written = build_runbook(args.out)
    except RunbookTooLong as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"wrote {written} ({written.stat().st_size:,} bytes, 1 page)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
