"""Tests for the DraftKings CSV import path.

Two jobs. First, prove the happy path produces correct normalized records.
Second -- and more important for this project -- prove that every malformed
input fails with a message naming the file, row and column. A traceback is a
bug report; silence is a data-integrity failure.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dfs_pipeline.adapters import (
    DraftKingsCsvAdapter,
    SlateSchemaError,
    parse_game_info,
)
from dfs_pipeline.capture import ingest_slate
from dfs_pipeline.store import SnapshotStore

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def adapter():
    return DraftKingsCsvAdapter(FIXTURES / "dk_salaries_good.csv")


@pytest.fixture()
def store(tmp_path):
    with SnapshotStore.open(tmp_path / "snapshots.sqlite") as s:
        yield s


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_loads_every_row(adapter):
    players = adapter.load()
    assert len(players) == 13


def test_parses_a_player_correctly(adapter):
    mahomes = next(p for p in adapter.load() if p.name == "Patrick Mahomes")
    assert mahomes.source_player_id == "39971296"
    assert mahomes.position == "QB"
    assert mahomes.salary == 8200
    assert mahomes.team == "KC"
    assert mahomes.avg_points_per_game == pytest.approx(24.31)
    assert mahomes.entity_type == "player"
    assert mahomes.is_defense is False
    assert mahomes.opponent == "BUF"


def test_defenses_are_a_distinct_entity_type(adapter):
    defenses = [p for p in adapter.load() if p.is_defense]
    assert len(defenses) == 2
    assert {d.name for d in defenses} == {"Eagles", "Chiefs"}
    assert all(d.entity_type == "dst" for d in defenses)


def test_flex_eligibility_is_captured(adapter):
    pacheco = next(p for p in adapter.load() if p.name == "Isiah Pacheco")
    assert pacheco.roster_positions == ("RB", "FLEX")
    mahomes = next(p for p in adapter.load() if p.name == "Patrick Mahomes")
    assert mahomes.roster_positions == ("QB",)


def test_api_only_fields_are_none_not_invented(adapter):
    """The CSV cannot know these. None is honest; a default would not be."""
    p = adapter.load()[0]
    assert p.draft_group_id is None
    assert p.lock_time_utc is None


def test_two_games_are_detected(adapter):
    assert {p.game.key for p in adapter.load()} == {"KC@BUF", "DAL@PHI"}


# ---------------------------------------------------------------------------
# Game info and timezone handling
# ---------------------------------------------------------------------------

def test_kickoff_converts_eastern_to_utc():
    """September is EDT (UTC-4), so 4:25 PM ET is 20:25 UTC."""
    game = parse_game_info(
        "KC@BUF 09/13/2026 04:25PM ET", path="x.csv", row=2
    )
    assert game.away_team == "KC"
    assert game.home_team == "BUF"
    assert game.kickoff_utc == "2026-09-13T20:25:00Z"


def test_kickoff_handles_the_dst_boundary():
    """January is EST (UTC-5), so the same wall clock is a different UTC time.

    Hardcoding a fixed offset would silently shift every playoff slate by an
    hour, so this asserts the timezone database is actually doing the work.
    """
    january = parse_game_info("KC@BUF 01/11/2026 04:25PM ET", path="x.csv", row=2)
    assert january.kickoff_utc == "2026-01-11T21:25:00Z"


def test_game_without_a_time_is_allowed():
    """DraftKings mangles this for postponed games; the matchup still counts."""
    game = parse_game_info("KC@BUF", path="x.csv", row=2)
    assert game.key == "KC@BUF"
    assert game.kickoff_utc is None


def test_opponent_resolution():
    game = parse_game_info("KC@BUF", path="x.csv", row=2)
    assert game.opponent_of("KC") == "BUF"
    assert game.opponent_of("BUF") == "KC"
    assert game.opponent_of("SEA") is None


# ---------------------------------------------------------------------------
# Malformed inputs: every one must fail specifically
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "fixture, fragment",
    [
        ("dk_salaries_missing_column.csv", "missing required column"),
        ("dk_salaries_empty_salary.csv", "value is empty"),
        ("dk_salaries_nonnumeric_salary.csv", "is not a number"),
        ("dk_salaries_duplicate_id.csv", "duplicate player ID"),
        ("dk_salaries_one_game.csv", "only one game present"),
        ("dk_salaries_bad_game_info.csv", "could not parse game info"),
        ("dk_salaries_team_not_in_game.csv", "does not appear in its own game"),
        ("dk_salaries_header_only.csv", "no usable player rows"),
        ("dk_salaries_empty.csv", "file is empty"),
    ],
)
def test_malformed_input_fails_with_a_specific_message(fixture, fragment):
    with pytest.raises(SlateSchemaError, match=fragment):
        DraftKingsCsvAdapter(FIXTURES / fixture).load()


def test_error_names_the_file_and_row():
    """'Invalid CSV' is useless against a 700-row file."""
    with pytest.raises(SlateSchemaError) as exc:
        DraftKingsCsvAdapter(FIXTURES / "dk_salaries_empty_salary.csv").load()
    message = str(exc.value)
    assert "dk_salaries_empty_salary.csv" in message
    assert ":2" in message, "row number missing"
    assert "Salary" in message, "column name missing"


def test_missing_file_fails_clearly(tmp_path):
    with pytest.raises(SlateSchemaError, match="does not exist"):
        DraftKingsCsvAdapter(tmp_path / "nope.csv").load()


def test_directory_instead_of_file_fails_clearly(tmp_path):
    with pytest.raises(SlateSchemaError, match="directory"):
        DraftKingsCsvAdapter(tmp_path).load()


def test_missing_column_message_lists_what_was_found():
    """The operator needs to see the actual header to diagnose it."""
    with pytest.raises(SlateSchemaError) as exc:
        DraftKingsCsvAdapter(FIXTURES / "dk_salaries_missing_column.csv").load()
    assert "Found:" in str(exc.value)
    assert "Game Info" in str(exc.value)


def test_additive_schema_changes_are_tolerated(tmp_path):
    """DraftKings adds columns over time without removing ours.

    Requiring an exact header match would break the pipeline on a harmless
    change; requiring a subset catches real breakage without false alarms.
    """
    original = (FIXTURES / "dk_salaries_good.csv").read_text().splitlines()
    header = original[0] + ",SomeNewColumn"
    rows = [r + ",whatever" for r in original[1:]]
    target = tmp_path / "future.csv"
    target.write_text("\n".join([header, *rows]) + "\n")

    assert len(DraftKingsCsvAdapter(target).load()) == 13


def test_utf8_bom_is_handled(tmp_path):
    """Excel writes a BOM, and operators open these files in Excel."""
    raw = (FIXTURES / "dk_salaries_good.csv").read_bytes()
    target = tmp_path / "bom.csv"
    target.write_bytes(b"\xef\xbb\xbf" + raw)
    assert len(DraftKingsCsvAdapter(target).load()) == 13


def test_trailing_blank_line_is_ignored(tmp_path):
    target = tmp_path / "trailing.csv"
    target.write_text((FIXTURES / "dk_salaries_good.csv").read_text() + "\n\n")
    assert len(DraftKingsCsvAdapter(target).load()) == 13


def test_all_blank_row_is_skipped_not_rejected(tmp_path):
    """A row of empty commas is padding, not corruption.

    Distinct from the trailing-newline case: csv.DictReader drops a bare
    empty line before we ever see it, but a comma-only row arrives as a real
    record with every field blank.
    """
    lines = (FIXTURES / "dk_salaries_good.csv").read_text().splitlines()
    lines.insert(3, "," * 8)
    target = tmp_path / "padded.csv"
    target.write_text("\n".join(lines) + "\n")
    assert len(DraftKingsCsvAdapter(target).load()) == 13


def test_zero_salary_is_rejected(tmp_path):
    """DraftKings salaries are positive; a zero means the export is broken."""
    lines = (FIXTURES / "dk_salaries_good.csv").read_text().splitlines()
    lines[1] = lines[1].replace(",8200,", ",0,")
    target = tmp_path / "zero.csv"
    target.write_text("\n".join(lines) + "\n")
    with pytest.raises(SlateSchemaError, match="salaries are positive"):
        DraftKingsCsvAdapter(target).load()


def test_missing_average_points_is_none_not_zero(tmp_path):
    """Absent is not the same as zero, and conflating them corrupts a mean."""
    lines = (FIXTURES / "dk_salaries_good.csv").read_text().splitlines()
    lines[1] = lines[1].rsplit(",", 1)[0] + ","
    target = tmp_path / "no_avg.csv"
    target.write_text("\n".join(lines) + "\n")
    player = DraftKingsCsvAdapter(target).load()[0]
    assert player.avg_points_per_game is None


def test_nonnumeric_average_points_is_rejected(tmp_path):
    lines = (FIXTURES / "dk_salaries_good.csv").read_text().splitlines()
    lines[1] = lines[1].rsplit(",", 1)[0] + ",not-a-number"
    target = tmp_path / "bad_avg.csv"
    target.write_text("\n".join(lines) + "\n")
    with pytest.raises(SlateSchemaError, match="AvgPointsPerGame"):
        DraftKingsCsvAdapter(target).load()


def test_roster_position_column_is_optional(tmp_path):
    """It is useful, not required; its absence must not break ingest."""
    lines = (FIXTURES / "dk_salaries_good.csv").read_text().splitlines()
    header = lines[0].split(",")
    idx = header.index("Roster Position")
    stripped = [
        ",".join(v for i, v in enumerate(line.split(",")) if i != idx)
        for line in lines
    ]
    target = tmp_path / "no_roster.csv"
    target.write_text("\n".join(stripped) + "\n")
    players = DraftKingsCsvAdapter(target).load()
    assert len(players) == 13
    assert all(p.roster_positions == () for p in players)


def test_lock_time_is_recorded_when_a_source_supplies_it():
    """The CSV cannot know lock times, but the future API adapter can.

    Exercised directly because no CSV fixture can reach this path, and an
    untested branch waiting for the API adapter is a branch that breaks then.
    """
    from dfs_pipeline.adapters.base import GameInfo, SlatePlayer
    from dfs_pipeline.capture import _observations_for

    player = SlatePlayer(
        source_player_id="1", name="Test", position="QB", salary=8000,
        team="KC", game=GameInfo("KC", "BUF", None), entity_type="player",
        lock_time_utc="2026-09-13T17:00:00Z",
    )
    metrics = {
        o["metric"]: o["value"]
        for o in _observations_for([player], "DK_API", "t", "t")
    }
    assert metrics["dk_lock_time"] == "2026-09-13T17:00:00Z"


# ---------------------------------------------------------------------------
# Against real DraftKings output
# ---------------------------------------------------------------------------
# dk_salaries_real_shape.csv is a 27-row subset of an actual 2026 Week 1 main
# slate export, preserving every structural feature of the original: the UTF-8
# BOM, CRLF line endings, the Status column, apostrophes and suffixes in
# names, and multiple games. The earlier fixtures test what we assumed; this
# one tests what DraftKings actually emits.

REAL_SHAPE = FIXTURES / "dk_salaries_real_shape.csv"


def test_real_export_shape_parses():
    players = DraftKingsCsvAdapter(REAL_SHAPE).load()
    assert len(players) == 27


def test_real_export_has_bom_and_crlf():
    """Guards the fixture itself. If it loses these, it stops testing them."""
    raw = REAL_SHAPE.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf", "BOM missing from fixture"
    assert b"\r\n" in raw, "CRLF line endings missing from fixture"


def test_status_column_is_captured():
    """DraftKings emits Q / IR / OUT -- notably never DOUBTFUL."""
    players = DraftKingsCsvAdapter(REAL_SHAPE).load()
    statuses = {p.status for p in players if p.status}
    assert statuses <= {"Q", "IR", "OUT"}
    assert "OUT" in statuses and "IR" in statuses and "Q" in statuses
    assert "DOUBTFUL" not in statuses


def test_unflagged_players_have_no_status():
    players = DraftKingsCsvAdapter(REAL_SHAPE).load()
    clean = [p for p in players if not p.is_flagged]
    assert clean, "fixture should contain unflagged players"
    assert all(p.status is None for p in clean)


def test_status_is_captured_not_acted_on():
    """The adapter must not silently drop injured players.

    Whether to exclude a status is the optimizer's decision. Filtering here
    would discard point-in-time data that cannot be recovered later -- a
    player's designation at capture time is itself an observation.
    """
    players = DraftKingsCsvAdapter(REAL_SHAPE).load()
    assert any(p.status == "OUT" for p in players), "OUT players were dropped"
    assert any(p.status == "IR" for p in players), "IR players were dropped"


def test_names_with_apostrophes_and_suffixes_survive():
    """These are the identity-resolution hazards, and they must arrive intact."""
    names = {p.name for p in DraftKingsCsvAdapter(REAL_SHAPE).load()}
    assert any("'" in n for n in names), "no apostrophe names in fixture"
    assert any(n.endswith(("Jr.", "Sr.", "III")) for n in names), "no suffixes"


def test_real_defenses_use_team_nicknames():
    """DK names defenses by nickname while TeamAbbrev carries the code."""
    defenses = [p for p in DraftKingsCsvAdapter(REAL_SHAPE).load() if p.is_defense]
    assert defenses
    for d in defenses:
        assert d.entity_type == "dst"
        assert d.name != d.team, f"expected a nickname, got {d.name!r}"


def test_status_reaches_the_store(store):
    ingest_slate(
        store, DraftKingsCsvAdapter(REAL_SHAPE), captured_at="2026-09-11T18:00:00Z"
    )
    flagged = store.as_of("2026-09-12T23:00:00Z", metric="dk_status")
    assert {o.value for o in flagged} <= {"Q", "IR", "OUT"}
    assert len(flagged) >= 5


# ---------------------------------------------------------------------------
# Capture into the store
# ---------------------------------------------------------------------------

def test_ingest_records_observations_and_artifact(store, adapter):
    result = ingest_slate(
        store, adapter, original_filename="DKSalaries.csv",
        captured_at="2026-09-11T18:00:00Z",
    )
    assert result.players == 11
    assert result.defenses == 2
    assert result.total_entries == 13
    assert result.games == 2
    assert result.observations == store.observation_count()
    assert store.artifact_count() == 1


def test_ingested_salaries_are_queryable_as_of_a_cutoff(store, adapter):
    ingest_slate(store, adapter, captured_at="2026-09-11T18:00:00Z")

    salaries = store.as_of("2026-09-12T23:00:00Z", metric="dk_salary")
    assert len(salaries) == 13
    by_id = {o.source_subject_id: o.value for o in salaries}
    assert by_id["39971296"] == 8200.0

    # Nothing is knowable before the capture happened.
    assert store.as_of("2026-09-10T00:00:00Z", metric="dk_salary") == []


def test_effective_at_defaults_to_captured_at(store, adapter):
    """A manual CSV carries no self-reported timestamp.

    Claiming to know when the salaries were set would be inventing
    information. 'Current as of capture' is the honest statement.
    """
    result = ingest_slate(store, adapter, captured_at="2026-09-11T18:00:00Z")
    assert result.effective_at == result.captured_at == "2026-09-11T18:00:00Z"


def test_backfill_can_state_an_earlier_effective_time(store, adapter):
    result = ingest_slate(
        store, adapter,
        effective_at="2026-09-10T12:00:00Z",
        captured_at="2026-09-13T09:00:00Z",
    )
    assert result.effective_at == "2026-09-10T12:00:00Z"
    assert result.captured_at == "2026-09-13T09:00:00Z"
    # Effective Thursday but not captured until Sunday: invisible on Saturday.
    assert store.as_of("2026-09-12T23:00:00Z", metric="dk_salary") == []


def test_artifact_is_stored_before_parsing(store, tmp_path):
    """A parse failure must still leave the raw bytes archived.

    The week's slate cannot be downloaded again; an unparsed artifact is
    recoverable, an unarchived one is not.
    """
    bad = tmp_path / "bad.csv"
    bad.write_text(
        (FIXTURES / "dk_salaries_one_game.csv").read_text()
    )
    with pytest.raises(SlateSchemaError):
        ingest_slate(store, DraftKingsCsvAdapter(bad))
    assert store.artifact_count() == 1, "artifact was lost on parse failure"
    assert store.observation_count() == 0


def test_reingesting_the_same_file_is_rejected_by_default(store, adapter):
    """Re-running an already-complete capture is a mistake worth surfacing."""
    ingest_slate(store, adapter, captured_at="2026-09-11T18:00:00Z")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        ingest_slate(store, adapter, captured_at="2026-09-11T18:00:00Z")


def test_reingest_can_be_made_idempotent(store, adapter):
    ingest_slate(store, adapter, captured_at="2026-09-11T18:00:00Z")
    before = store.observation_count()
    ingest_slate(
        store, adapter, captured_at="2026-09-11T18:00:00Z", on_duplicate="ignore"
    )
    assert store.observation_count() == before
    assert store.artifact_count() == 1, "identical bytes should not duplicate"


def test_recapture_later_creates_new_history(store, adapter):
    """Salaries do not change mid-week, but a second capture is still history."""
    ingest_slate(store, adapter, captured_at="2026-09-11T18:00:00Z")
    ingest_slate(store, adapter, captured_at="2026-09-12T18:00:00Z")
    assert store.artifact_count() == 1, "same bytes, one artifact"
    salaries = store.as_of("2026-09-13T00:00:00Z", metric="dk_salary")
    assert len(salaries) == 13, "as-of must still resolve one row per player"


def test_stored_artifact_reparses_identically(store, adapter):
    """Re-parsing the archived bytes must reproduce the original records.

    This is what makes the raw zone worth keeping: a parser fixed in December
    can be re-run against September's untouched bytes.
    """
    result = ingest_slate(store, adapter, captured_at="2026-09-11T18:00:00Z")
    archived = store.artifact_bytes(result.artifact_sha256)
    assert adapter.loads(archived) == adapter.load()
