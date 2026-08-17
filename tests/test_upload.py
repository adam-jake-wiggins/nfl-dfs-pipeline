"""Golden-file tests for the DraftKings bulk-upload format.

The fixture is a trimmed copy of a real DraftKings upload template downloaded
2026-08-17, retaining DraftKings' own instruction rows. Those instructions are
the authority here -- every assertion below quotes the template rather than
restating a belief about it, which is the whole point of the exercise: the
prototype's format was a guess that happened to be right, and "happened to be
right" is not a status this project accepts.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from dfs_pipeline.contest import ROSTER_SIZE, SLOT_ORDER
from dfs_pipeline.upload import (
    MAX_LINEUPS_PER_FILE,
    UPLOAD_HEADER,
    UploadError,
    format_cell,
    write_upload_csv,
)

FIXTURES = Path(__file__).parent / "fixtures"
TEMPLATE = FIXTURES / "dk_upload_template.csv"


def template_rows() -> list[list[str]]:
    return list(csv.reader(TEMPLATE.open(newline="", encoding="utf-8-sig")))


LINEUP = [
    ("Patrick Mahomes", "39971296"),
    ("Isiah Pacheco", "39971301"),
    ("James Cook", "39971302"),
    ("Rashee Rice", "39971310"),
    ("Khalil Shakir", "39971311"),
    ("CeeDee Lamb", "39971320"),
    ("Travis Kelce", "39971330"),
    ("Saquon Barkley", "39971340"),
    ("Eagles", "39971400"),
]


# ---------------------------------------------------------------------------
# Golden: our header must equal DraftKings' own
# ---------------------------------------------------------------------------

def test_our_header_matches_the_real_template(tmp_path):
    """The prototype's assumption, finally checked against the real thing."""
    target = tmp_path / "lineups.csv"
    write_upload_csv([LINEUP], target)

    written = next(csv.reader(target.open(newline="", encoding="utf-8")))
    expected = template_rows()[0][:ROSTER_SIZE]

    assert expected == ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST"]
    assert written == expected


def test_slot_order_constant_matches_the_template():
    assert list(SLOT_ORDER) == template_rows()[0][:ROSTER_SIZE]
    assert UPLOAD_HEADER == SLOT_ORDER


def test_the_template_carries_a_blank_spacer_then_the_salary_listing():
    """Guards the fixture's structure so later assertions stay meaningful."""
    header = template_rows()[0]
    assert header[9] == "", "expected a blank spacer column"
    assert header[10] == "Instructions"


def test_the_salary_block_header_is_where_we_think():
    assert template_rows()[7][10:] == [
        "Position", "Name + ID", "Name", "ID",
        "Roster Position", "Salary", "Game Info", "TeamAbbrev",
        "AvgPointsPerGame",
    ]


# ---------------------------------------------------------------------------
# What DraftKings' instructions actually say
# ---------------------------------------------------------------------------

def _instructions() -> str:
    return " ".join(
        row[10] for row in template_rows()[:7] if len(row) > 10 and row[10].strip()
    ).lower()


def test_instructions_permit_the_name_and_id_form():
    """'you can use the Name + ID column or the ID column'.

    Our `Name (ID)` output is literally the Name + ID column's own format.
    """
    text = _instructions()
    assert "name + id column or the id column" in text


def test_instructions_forbid_a_bare_name():
    assert "cannot use just the player's name" in _instructions()


def test_the_name_and_id_column_has_the_shape_we_write():
    """Template value: 'C.J. Stroud (43837771)'."""
    data = [r for r in template_rows()[8:] if len(r) > 13 and r[13].strip()]
    assert data
    for row in data[:5]:
        assert row[11] == f"{row[12]} ({row[13]})"

    assert format_cell("C.J. Stroud", "43837771") == "C.J. Stroud (43837771)"


def test_five_hundred_lineup_limit_is_stated_in_the_template():
    """A constraint we did not know about until we read the real file."""
    assert "up to 500 lineups per file" in _instructions()
    assert MAX_LINEUPS_PER_FILE == 500


def test_one_id_per_player_serves_every_slot_including_flex():
    """The salary block lists each player once with a combined roster position.

    So the FLEX row takes that same id. The draftables API *does* issue a
    separate FLEX draftableId, and using it here would be a plausible mistake
    with no way to notice short of a failed upload.
    """
    data = [r for r in template_rows()[8:] if len(r) > 14 and r[13].strip()]
    ids = [r[13] for r in data]
    assert len(ids) == len(set(ids)), "a player appears twice with different ids"

    flex_eligible = [r for r in data if "FLEX" in r[14]]
    assert flex_eligible, "fixture should contain flex-eligible players"
    for row in flex_eligible:
        assert "/" in row[14], "expected a combined roster position like RB/FLEX"


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def test_a_lineup_writes_nine_cells(tmp_path):
    target = tmp_path / "out.csv"
    assert write_upload_csv([LINEUP], target) == 1

    rows = list(csv.reader(target.open(newline="", encoding="utf-8")))
    assert len(rows) == 2
    assert len(rows[1]) == ROSTER_SIZE
    assert rows[1][0] == "Patrick Mahomes (39971296)"
    assert rows[1][-1] == "Eagles (39971400)"


def test_multiple_lineups_write_one_row_each(tmp_path):
    target = tmp_path / "out.csv"
    assert write_upload_csv([LINEUP, LINEUP, LINEUP], target) == 3
    assert len(list(csv.reader(target.open(newline="", encoding="utf-8")))) == 4


def test_the_five_hundred_limit_is_refused_before_writing(tmp_path):
    """Rejected at upload is the worst possible time to learn this."""
    target = tmp_path / "out.csv"
    with pytest.raises(UploadError, match="exceeds DraftKings' limit"):
        write_upload_csv([LINEUP] * (MAX_LINEUPS_PER_FILE + 1), target)
    assert not target.exists(), "nothing should have been written"


def test_exactly_five_hundred_is_allowed(tmp_path):
    target = tmp_path / "out.csv"
    assert write_upload_csv([LINEUP] * MAX_LINEUPS_PER_FILE, target) == 500


def test_a_short_lineup_is_refused(tmp_path):
    with pytest.raises(UploadError, match="has 8 players, expected 9"):
        write_upload_csv([LINEUP[:-1]], tmp_path / "out.csv")


def test_an_empty_set_is_refused(tmp_path):
    with pytest.raises(UploadError, match="no lineups"):
        write_upload_csv([], tmp_path / "out.csv")


def test_a_missing_id_is_refused_not_written_blank():
    """DraftKings rejects a name alone; a blank id makes a file that looks
    complete and uploads as broken."""
    with pytest.raises(UploadError, match="no DraftKings id"):
        format_cell("Some Player", "")


def test_a_missing_id_aborts_the_whole_file(tmp_path):
    target = tmp_path / "out.csv"
    broken = LINEUP[:-1] + [("Eagles", "")]
    with pytest.raises(UploadError):
        write_upload_csv([broken], target)


def test_an_id_without_a_name_still_writes(tmp_path):
    """Instruction 2 permits the bare ID column, so an id alone is valid."""
    assert format_cell("", "39971296") == "39971296"


def test_names_with_punctuation_survive(tmp_path):
    """Apostrophes and periods are common and must not be mangled."""
    target = tmp_path / "out.csv"
    lineup = [("Ja'Marr Chase", "1"), ("C.J. Stroud", "2")] + [
        (f"P{i}", str(i)) for i in range(3, 10)
    ]
    write_upload_csv([lineup], target)
    rows = list(csv.reader(target.open(newline="", encoding="utf-8")))
    assert rows[1][0] == "Ja'Marr Chase (1)"
    assert rows[1][1] == "C.J. Stroud (2)"


def test_integer_ids_are_accepted():
    assert format_cell("Player", 39971296) == "Player (39971296)"


def test_whitespace_is_trimmed():
    assert format_cell("  Player  ", "  123  ") == "Player (123)"


def test_output_is_readable_by_a_plain_csv_reader(tmp_path):
    """A reviewer who cannot open the file cannot catch a mistake in it."""
    target = tmp_path / "out.csv"
    write_upload_csv([LINEUP], target)
    # Read bytes: read_text() applies universal-newline translation and would
    # hide the line terminator the file actually carries.
    raw = target.read_bytes()
    assert raw.startswith(b"QB,RB,RB,WR,WR,WR,TE,FLEX,DST\r\n"), (
        "csv.writer's excel dialect uses CRLF, which is what DraftKings' own "
        "template uses too"
    )
    assert b"Patrick Mahomes (39971296)" in raw
