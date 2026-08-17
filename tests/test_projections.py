"""Tests for name normalization and projection-CSV ingestion.

Fixtures are built from the real player names in a DraftKings export, so the
name layer is exercised against spellings that actually occur -- apostrophes,
hyphens, initials and generational suffixes -- even though the DFF column
layout itself remains UNVERIFIED until a real export arrives.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest
from hypothesis import given, strategies as st

from dfs_pipeline.adapters import ProjectionsCsvAdapter, SlateSchemaError
from dfs_pipeline.capture import ingest_projections
from dfs_pipeline.names import NAME_SUFFIXES, name_key, normalize_name
from dfs_pipeline.store import SnapshotStore

FIXTURES = Path(__file__).parent / "fixtures"
DFF = FIXTURES / "projections_dff_shape.csv"
MINIMAL = FIXTURES / "projections_minimal.csv"


@pytest.fixture()
def store(tmp_path):
    with SnapshotStore.open(tmp_path / "snapshots.sqlite") as s:
        yield s


@pytest.fixture()
def adapter():
    return ProjectionsCsvAdapter(DFF, source_name="DFF")


# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Ja'Marr Chase", "jamarr chase"),
        ("Amon-Ra St. Brown", "amon ra st brown"),
        ("Travis Etienne Jr.", "travis etienne"),
        ("C.J. Stroud", "cj stroud"),
        ("Jacory Croskey-Merritt", "jacory croskey merritt"),
        ("Aaron Jones Sr.", "aaron jones"),
        ("De'Von Achane", "devon achane"),
        ("Robert Griffin III", "robert griffin"),
        ("  Josh   Allen  ", "josh allen"),
        ("JOSH ALLEN", "josh allen"),
    ],
)
def test_normalization_of_real_spellings(raw, expected):
    assert normalize_name(raw) == expected


@pytest.mark.parametrize(
    "a, b",
    [
        ("Ja'Marr Chase", "JaMarr Chase"),
        ("C.J. Stroud", "CJ Stroud"),
        ("Travis Etienne Jr.", "Travis Etienne"),
        ("Jacory Croskey-Merritt", "Jacory Croskey Merritt"),
        ("Amon-Ra St. Brown", "Amon Ra St Brown"),
        ("Michael Pittman Jr.", "Michael Pittman"),
    ],
)
def test_variant_spellings_agree(a, b):
    """Sources spell the same person differently; the key must not care."""
    assert normalize_name(a) == normalize_name(b)


def test_accents_are_stripped():
    assert normalize_name("Nuñez") == normalize_name("Nunez")


def test_a_suffix_is_never_the_entire_name():
    """A lone token that looks like a suffix is somebody's name."""
    assert normalize_name("Jr") == "jr"
    assert normalize_name("V") == "v"


def test_only_trailing_suffixes_are_stripped():
    """'Ivy' and 'Roman' must survive; only a final Jr/Sr/III goes."""
    assert normalize_name("Jr Smith") == "jr smith"
    assert normalize_name("Smith Jr") == "smith"


def test_empty_and_punctuation_only_names_normalize_to_nothing():
    for value in ("", "   ", "...", "---"):
        assert normalize_name(value) == ""


def test_suffix_list_is_lowercase():
    """The comparison happens after casefolding, so the table must match."""
    assert all(s == s.lower() for s in NAME_SUFFIXES)


@given(st.text(min_size=1, max_size=40))
def test_normalization_is_idempotent(raw):
    once = normalize_name(raw)
    assert normalize_name(once) == once


@given(st.text(min_size=1, max_size=40))
def test_normalization_never_returns_leading_or_trailing_space(raw):
    result = normalize_name(raw)
    assert result == result.strip()
    assert "  " not in result


def test_name_key_disambiguates_with_team_and_position():
    assert name_key("Michael Thomas") == "michael thomas"
    assert name_key("Michael Thomas", "NO") == "michael thomas|NO"
    assert name_key("Michael Thomas", "NO", "wr") == "michael thomas|NO|WR"


def test_normalization_does_not_merge_real_players():
    """The safety property: 692 real players, zero collisions.

    If normalization ever became aggressive enough to merge two people, this
    is where it would show up -- and merging is worse than failing to match,
    because it silently attaches one player's projection to another's salary.
    """
    raw = (FIXTURES / "dk_salaries_real_shape.csv").read_bytes().decode("utf-8-sig")
    names = [r["Name"] for r in csv.DictReader(io.StringIO(raw)) if r["Position"] != "DST"]
    keys = [normalize_name(n) for n in names]
    assert len(set(keys)) == len(keys), "normalization merged distinct players"


# ---------------------------------------------------------------------------
# Reading a DFF-shaped file
# ---------------------------------------------------------------------------

def test_split_first_and_last_name_columns_are_joined(adapter):
    rows = adapter.load()
    assert any(r.name == "Ja'Marr Chase" for r in rows)


def test_dk_prefixed_columns_are_preferred(adapter):
    """A file carrying both DK and FanDuel columns must not use the wrong one."""
    rows = adapter.load()
    assert all(r.projection > 0 for r in rows)


def test_ownership_percentage_sign_is_tolerated(adapter):
    rows = adapter.load()
    owned = [r for r in rows if r.ownership is not None]
    assert owned, "fixture should carry ownership"
    assert all(0 <= r.ownership <= 100 for r in owned)


def test_optional_fields_are_captured(adapter):
    row = next(r for r in adapter.load() if r.name == "Ja'Marr Chase")
    assert row.position == "WR"
    assert row.team == "CIN"
    assert row.salary and row.salary > 0


def test_source_name_is_normalized_and_required():
    assert ProjectionsCsvAdapter(DFF, source_name=" dff ").source_name == "DFF"
    with pytest.raises(ValueError, match="source_name is required"):
        ProjectionsCsvAdapter(DFF, source_name="  ")


def test_minimal_file_with_only_name_and_projection_works():
    rows = ProjectionsCsvAdapter(MINIMAL, source_name="ANY").load()
    assert len(rows) == 22
    assert all(r.position is None and r.team is None for r in rows)
    assert all(r.ownership is None for r in rows)


def test_original_spelling_is_preserved_alongside_the_key(adapter):
    """The key is for joining; the original is what a human needs on a miss."""
    row = next(r for r in adapter.load() if r.normalized_name == "jamarr chase")
    assert row.name == "Ja'Marr Chase"


# ---------------------------------------------------------------------------
# Failures, each specific
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "fixture, fragment",
    [
        ("projections_no_name_column.csv", "no player-name column"),
        ("projections_no_projection_column.csv", "no projection column"),
        ("projections_bad_number.csv", "is not a number"),
        ("projections_header_only.csv", "no usable projection rows"),
        ("projections_ambiguous_name.csv", "cannot be separated"),
    ],
)
def test_malformed_files_fail_specifically(fixture, fragment):
    with pytest.raises(SlateSchemaError, match=fragment):
        ProjectionsCsvAdapter(FIXTURES / fixture, source_name="TEST").load()


def test_missing_column_error_lists_what_it_looked_for_and_found():
    with pytest.raises(SlateSchemaError) as exc:
        ProjectionsCsvAdapter(
            FIXTURES / "projections_no_projection_column.csv", source_name="TEST"
        ).load()
    message = str(exc.value)
    assert "Looked for" in message and "Found:" in message


def test_shared_names_separable_by_team_are_kept_apart():
    """Two real players can share a name; team and position separate them."""
    rows = ProjectionsCsvAdapter(
        FIXTURES / "projections_separable_name.csv", source_name="TEST"
    ).load()
    assert len(rows) == 2
    assert len({r.subject_key for r in rows}) == 2
    assert all("michael thomas|" in r.subject_key for r in rows)


def test_shared_names_that_cannot_be_separated_raise_rather_than_merge():
    """Merging would attach one player's projection to another's salary."""
    with pytest.raises(SlateSchemaError, match="Michael Thomas"):
        ProjectionsCsvAdapter(
            FIXTURES / "projections_ambiguous_name.csv", source_name="TEST"
        ).load()


def test_missing_file_fails_clearly(tmp_path):
    with pytest.raises(SlateSchemaError, match="does not exist"):
        ProjectionsCsvAdapter(tmp_path / "nope.csv", source_name="TEST").load()


def test_directory_fails_clearly(tmp_path):
    with pytest.raises(SlateSchemaError, match="directory"):
        ProjectionsCsvAdapter(tmp_path, source_name="TEST").load()


def test_empty_file_fails_clearly(tmp_path):
    target = tmp_path / "empty.csv"
    target.write_bytes(b"")
    with pytest.raises(SlateSchemaError, match="file is empty"):
        ProjectionsCsvAdapter(target, source_name="TEST").load()


def test_blank_name_is_rejected(tmp_path):
    target = tmp_path / "blank.csv"
    target.write_text("Name,Projection\n,12.5\n")
    with pytest.raises(SlateSchemaError, match="name is empty"):
        ProjectionsCsvAdapter(target, source_name="TEST").load()


def test_name_that_normalizes_to_nothing_is_rejected(tmp_path):
    target = tmp_path / "punct.csv"
    target.write_text("Name,Projection\n...,12.5\n")
    with pytest.raises(SlateSchemaError, match="normalizes to nothing"):
        ProjectionsCsvAdapter(target, source_name="TEST").load()


def test_trailing_blank_line_is_ignored(tmp_path):
    target = tmp_path / "trailing.csv"
    target.write_text(MINIMAL.read_text() + "\n\n")
    assert len(ProjectionsCsvAdapter(target, source_name="TEST").load()) == 22


def test_bom_is_handled(tmp_path):
    target = tmp_path / "bom.csv"
    target.write_bytes(b"\xef\xbb\xbf" + MINIMAL.read_bytes())
    assert len(ProjectionsCsvAdapter(target, source_name="TEST").load()) == 22


# ---------------------------------------------------------------------------
# Capture into the store
# ---------------------------------------------------------------------------

def test_projections_are_recorded(store, adapter):
    result = ingest_projections(
        store, adapter, original_filename="dff.csv",
        captured_at="2026-09-11T18:00:00Z",
    )
    assert result.source == "DFF"
    assert result.rows == 22
    assert result.with_ownership == 22
    assert result.observations == store.observation_count()
    assert store.artifact_count() == 1


def test_stated_timestamp_becomes_effective_at(store, adapter):
    """The DFF-shaped fixture states a slate_date; that is a real effective_at."""
    result = ingest_projections(store, adapter, captured_at="2026-09-13T09:00:00Z")
    assert result.effective_at == "2026-09-11T18:00:00Z"
    assert result.captured_at == "2026-09-13T09:00:00Z"
    assert result.effective_at != result.captured_at


def test_effective_at_defaults_to_captured_at_without_a_stated_time(store):
    """Most exports say nothing about when they were computed."""
    adapter = ProjectionsCsvAdapter(MINIMAL, source_name="ANY")
    result = ingest_projections(store, adapter, captured_at="2026-09-11T18:00:00Z")
    assert result.effective_at == result.captured_at == "2026-09-11T18:00:00Z"


def test_explicit_effective_at_wins_over_a_stated_one(store, adapter):
    result = ingest_projections(
        store, adapter,
        effective_at="2026-09-10T00:00:00Z",
        captured_at="2026-09-13T09:00:00Z",
    )
    assert result.effective_at == "2026-09-10T00:00:00Z"


def test_unparseable_stated_timestamp_falls_back_with_a_warning(store, tmp_path, caplog):
    """A junk date column must not fail the capture nor invent a timestamp."""
    target = tmp_path / "baddate.csv"
    target.write_text("Name,Projection,Updated\nJosh Allen,22.4,last Tuesday\n")
    adapter = ProjectionsCsvAdapter(target, source_name="TEST")
    with caplog.at_level("WARNING"):
        result = ingest_projections(store, adapter, captured_at="2026-09-11T18:00:00Z")
    assert result.effective_at == "2026-09-11T18:00:00Z"
    assert "unparseable stated timestamp" in caplog.text


def test_ownership_is_captured_because_the_roadmap_depends_on_it(store, adapter):
    """Field simulation is downstream of ownership; unowned capture is useless."""
    ingest_projections(store, adapter, captured_at="2026-09-11T18:00:00Z")
    owned = store.as_of("2026-09-12T00:00:00Z", metric="projection_ownership")
    assert len(owned) == 22


def test_source_spelling_is_stored_next_to_the_key(store, adapter):
    ingest_projections(store, adapter, captured_at="2026-09-11T18:00:00Z")
    names = {o.value for o in
             store.as_of("2026-09-12T00:00:00Z", metric="projection_source_name")}
    assert "Ja'Marr Chase" in names


def test_two_sources_do_not_merge(store):
    """Vendors disagreeing is signal; a consensus at capture time destroys it."""
    ingest_projections(store, ProjectionsCsvAdapter(DFF, source_name="DFF"),
                       captured_at="2026-09-11T18:00:00Z")
    ingest_projections(store, ProjectionsCsvAdapter(MINIMAL, source_name="OTHER"),
                       captured_at="2026-09-11T18:00:00Z")
    points = store.as_of("2026-09-12T00:00:00Z", metric="projection_dk_points")
    sources = {o.source for o in points}
    assert sources == {"DFF", "OTHER"}
    assert len(points) == 44, "each source keeps its own series"


def test_reprojection_later_is_new_history(store, adapter):
    """Projections move all week; each capture must be preserved."""
    ingest_projections(store, adapter, effective_at="2026-09-11T18:00:00Z",
                       captured_at="2026-09-11T18:00:00Z")
    first = store.observation_count()
    ingest_projections(store, adapter, effective_at="2026-09-12T18:00:00Z",
                       captured_at="2026-09-12T18:00:00Z")
    assert store.observation_count() == first * 2
    # The as-of query still resolves one row per player.
    assert len(store.as_of("2026-09-13T00:00:00Z",
                           metric="projection_dk_points", source="DFF")) == 22


def test_projections_join_to_a_slate_by_normalized_name(store, adapter):
    """The payoff: a projection key must match a slate player's key.

    This is the join the prototype got wrong -- it exact-matched lowercased
    names and silently fell back to a season average on a miss.
    """
    from dfs_pipeline.adapters import DraftKingsCsvAdapter

    ingest_projections(store, adapter, captured_at="2026-09-11T18:00:00Z")
    projection_keys = {
        o.source_subject_id
        for o in store.as_of("2026-09-12T00:00:00Z", metric="projection_dk_points")
    }

    slate = DraftKingsCsvAdapter(FIXTURES / "dk_salaries_real_shape.csv").load()
    slate_keys = {normalize_name(p.name) for p in slate if not p.is_defense}

    matched = projection_keys & slate_keys
    assert matched, "no projection matched any slate player"


# ---------------------------------------------------------------------------
# Against a REAL Daily Fantasy Fuel export
# ---------------------------------------------------------------------------
# projections_dff_real.csv is an actual DFF export for the 2026 Week 1 slate,
# obtained 2026-08-17. It is small (9 rows) because the export was a filtered
# slice, but it is authoritative on the one thing fixtures cannot invent: the
# column names DFF actually emits.

DFF_REAL = FIXTURES / "projections_dff_real.csv"


def test_real_dff_export_parses():
    rows = ProjectionsCsvAdapter(DFF_REAL, source_name="DFF").load()
    assert len(rows) == 9


def test_dff_uses_ppg_projection_not_projection():
    """DFF's column is `ppg_projection`, which no obvious guess would produce.

    The first draft of this reader rejected the real file outright, and that
    rejection is what made the fix a one-line change: the error named every
    alias tried and every header present.
    """
    from dfs_pipeline.adapters.projections_csv import PROJECTION_COLUMNS

    header = DFF_REAL.read_text().splitlines()[0].lower()
    assert "ppg_projection" in header
    assert "ppg_projection" in PROJECTION_COLUMNS


def test_value_projection_is_never_read_as_the_projection():
    """`value_projection` is points per $1,000 of salary, not points.

    Reading it would yield numbers around 3.0 -- entirely plausible, entirely
    wrong. It is excluded from the alias list on purpose.
    """
    from dfs_pipeline.adapters.projections_csv import PROJECTION_COLUMNS

    assert "value_projection" in DFF_REAL.read_text().splitlines()[0]
    assert "value_projection" not in PROJECTION_COLUMNS


def test_dff_game_date_is_not_treated_as_effective_at():
    """`game_date` names when the games are played, not when the projection
    was computed. It sits in the FUTURE relative to capture, so using it as
    `effective_at` would assert we knew Sunday's numbers weeks early -- and
    the store's clock-skew CHECK would reject the row.
    """
    from dfs_pipeline.adapters.projections_csv import UPDATED_COLUMNS

    header = DFF_REAL.read_text().splitlines()[0].lower()
    assert "game_date" in header
    assert "game_date" not in UPDATED_COLUMNS
    assert "slate_date" not in UPDATED_COLUMNS

    rows = ProjectionsCsvAdapter(DFF_REAL, source_name="DFF").load()
    assert all(r.stated_effective_at is None for r in rows)


def test_dff_injury_status_is_captured():
    rows = ProjectionsCsvAdapter(DFF_REAL, source_name="DFF").load()
    assert {r.injury_status for r in rows} == {"O"}


def test_dff_apostrophe_names_normalize():
    rows = ProjectionsCsvAdapter(DFF_REAL, source_name="DFF").load()
    polk = next(r for r in rows if r.name == "Ja'Lynn Polk")
    assert polk.subject_key == "jalynn polk"


def test_dff_injury_status_reaches_the_store(store):
    adapter = ProjectionsCsvAdapter(DFF_REAL, source_name="DFF")
    ingest_projections(store, adapter, captured_at="2026-08-17T17:31:00Z")
    flagged = store.as_of("2026-08-17T18:00:00Z", metric="projection_injury_status")
    assert len(flagged) == 9
    assert {o.value for o in flagged} == {"O"}


def test_a_filtered_export_still_captures_cleanly(store):
    """This export is 9 out-players, not a projection set.

    Capture must not care: recording what the vendor actually served is the
    job, and the match report is where a thin file becomes visible.
    """
    adapter = ProjectionsCsvAdapter(DFF_REAL, source_name="DFF")
    result = ingest_projections(store, adapter, captured_at="2026-08-17T17:31:00Z")
    assert result.rows == 9
    assert result.with_ownership == 0, "DFF omitted ownership in this export"
    assert result.effective_at == result.captured_at
