"""Tests for the FantasyPros adapter.

Fixtures are trimmed slices of real FantasyPros exports obtained 2026-08-17,
preserving the exact headers -- including the repeated column names and the
blank spacer row -- because those are the two properties that make this format
dangerous to read naively.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from dfs_pipeline.adapters import (
    LAYOUTS,
    SEASON_AVERAGE_METRIC,
    FantasyProsCsvAdapter,
    SlateSchemaError,
)
from dfs_pipeline.capture import ingest_projections
from dfs_pipeline.scoring import OffenseStats, score_offense
from dfs_pipeline.store import SnapshotStore

FIXTURES = Path(__file__).parent / "fixtures"


def data_rows(position: str) -> list[list[str]]:
    """Real data rows only -- no header, no FantasyPros spacer row."""
    text = (FIXTURES / f"fantasypros_{position.lower()}.csv").read_text()
    rows = list(csv.reader(io.StringIO(text)))
    width = len(rows[0])
    return [
        r for r in rows[1:]
        if len(r) >= width and r[0].strip() and r[-1].strip() != "FPTS"
    ]


def fp(position: str) -> FantasyProsCsvAdapter:
    return FantasyProsCsvAdapter(
        FIXTURES / f"fantasypros_{position.lower()}.csv", position=position
    )


@pytest.fixture()
def store(tmp_path):
    with SnapshotStore.open(tmp_path / "snapshots.sqlite") as s:
        yield s


# ---------------------------------------------------------------------------
# The duplicate-column problem
# ---------------------------------------------------------------------------

def test_the_qb_header_really_does_repeat_column_names():
    """Guards the premise. If FantasyPros ever fixes this, the guard is moot.

    ATT, YDS and TDS each appear twice -- once for passing, once for rushing.
    """
    header = csv.reader(
        io.StringIO((FIXTURES / "fantasypros_qb.csv").read_text())
    ).__next__()
    assert header.count("ATT") == 2
    assert header.count("YDS") == 2
    assert header.count("TDS") == 2


def test_dictreader_would_silently_read_the_wrong_column():
    """Demonstrates the bug this adapter exists to avoid.

    csv.DictReader keeps the LAST duplicate, so 'YDS' yields rushing yards
    while the passing value -- an order of magnitude larger -- is discarded.
    """
    text = (FIXTURES / "fantasypros_qb.csv").read_text()
    row = next(r for r in csv.DictReader(io.StringIO(text)) if r["Player"].strip())
    positional = next(
        r for r in csv.reader(io.StringIO(text)) if r and r[0].strip() == row["Player"]
    )
    passing_yards, rushing_yards = positional[4], positional[8]

    assert row["YDS"] == rushing_yards, "DictReader kept the last duplicate"
    assert row["YDS"] != passing_yards
    assert float(passing_yards) > float(rushing_yards) * 3, "they differ hugely"


def test_positional_parsing_reads_passing_yards_correctly():
    """Jalen Hurts: 217.5 passing yards, not his 27.3 rushing yards."""
    rows = fp("QB").load()
    hurts = next(r for r in rows if r.name == "Jalen Hurts")
    # 217.5*0.04 + 1.5*4 - 0.5*1 + 27.3*0.1 + 0.6*6 - 0.2*1
    assert hurts.projection == pytest.approx(20.33, abs=0.01)


def test_headers_are_verified_exactly_before_parsing(tmp_path):
    """A renamed or reordered column must fail loudly, not shift every field."""
    target = tmp_path / "changed.csv"
    original = (FIXTURES / "fantasypros_qb.csv").read_text().splitlines()
    target.write_text("\n".join(['"Player","Team","ATT","CMP","YARDS"'] + original[1:]))
    with pytest.raises(SlateSchemaError, match="unexpected QB header"):
        FantasyProsCsvAdapter(target, position="QB").load()


def test_header_mismatch_shows_expected_and_found(tmp_path):
    target = tmp_path / "changed.csv"
    target.write_text('"Player","Team"\n"X","KC"\n')
    with pytest.raises(SlateSchemaError) as exc:
        FantasyProsCsvAdapter(target, position="RB").load()
    message = str(exc.value)
    assert "expected:" in message and "found:" in message


def test_every_layout_indexes_within_its_header():
    """Guards the layout table itself against an editing slip."""
    for position, layout in LAYOUTS.items():
        for field, index in layout.fields.items():
            assert 0 <= index < len(layout.header), (
                f"{position}.{field} index {index} outside header"
            )


# ---------------------------------------------------------------------------
# The half-PPR problem
# ---------------------------------------------------------------------------

def test_their_fpts_column_is_half_ppr_not_draftkings():
    """The finding that motivates re-scoring rather than reading FPTS.

    Every running back's published FPTS matches base + 0.5 * receptions.
    DraftKings is FULL PPR, so taking that column directly would under-project
    every pass-catcher by half a point per reception.

    Tested as a comparative fit rather than an absolute tolerance, because
    FantasyPros rounds every component to one decimal in the export while
    computing FPTS from unrounded values -- so reconstruction carries up to
    ~0.3 of rounding error. The claim is which hypothesis fits best, and
    half-PPR wins by an order of magnitude.
    """
    errors = {"standard": 0.0, "half": 0.0, "full": 0.0}
    for row in data_rows("RB")[:8]:
        _, _, _, ryd, rtd, rec, recyd, rectd, fl, fpts = row[:10]
        f = float
        base = f(ryd) * 0.1 + f(rtd) * 6 + f(recyd) * 0.1 + f(rectd) * 6 - f(fl) * 2
        errors["standard"] += abs(f(fpts) - base)
        errors["half"] += abs(f(fpts) - (base + f(rec) * 0.5))
        errors["full"] += abs(f(fpts) - (base + f(rec) * 1.0))

    assert min(errors, key=errors.get) == "half", errors
    assert errors["half"] * 4 < errors["full"], "half-PPR should fit far better"
    assert errors["half"] * 4 < errors["standard"]


def test_rescoring_exceeds_their_fpts_for_pass_catchers():
    """Full PPR must beat half PPR by roughly half a point per reception."""
    theirs = {r[0].strip(): float(r[-1]) for r in data_rows("WR")}
    for row in fp("WR").load():
        ours = row.projection
        published = theirs[row.name]
        if published > 5:  # ignore fringe players where rounding dominates
            assert ours > published, f"{row.name}: {ours} should exceed {published}"


def test_quarterbacks_barely_move_because_they_catch_nothing():
    """The delta should be a turnover-scoring artefact only, not a PPR one."""
    theirs = {r[0].strip(): float(r[-1]) for r in data_rows("QB")}
    for row in fp("QB").load():
        assert abs(row.projection - theirs[row.name]) < 1.0


def test_scoring_uses_the_canonical_module():
    """One definition of DraftKings scoring governs the whole project."""
    rows = fp("RB").load()
    gibbs = next(r for r in rows if r.name == "Jahmyr Gibbs")
    expected = score_offense(
        OffenseStats(
            rushing_yards=83.7, rushing_tds=0.9, receptions=3.9,
            receiving_yards=29.6, receiving_tds=0.2, fumbles_lost=0.1,
        )
    )
    assert gibbs.projection == pytest.approx(expected, abs=0.01)


# ---------------------------------------------------------------------------
# Per-position parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("position", ["QB", "RB", "WR", "TE", "DST"])
def test_every_position_parses(position):
    rows = fp(position).load()
    assert len(rows) == 12
    assert all(r.position == position for r in rows)
    assert all(r.projection is not None for r in rows)


def test_receiver_layout_differs_from_running_back():
    """WR lists receiving first, RB lists rushing first. Swapping them would
    read rushing attempts as receptions -- plausible, and wrong."""
    assert LAYOUTS["RB"].fields["rushing_yards"] == 3
    assert LAYOUTS["WR"].fields["receiving_yards"] == 3


def test_the_blank_spacer_row_is_skipped():
    """FantasyPros emits a short blank row after the header."""
    text = (FIXTURES / "fantasypros_qb.csv").read_text().splitlines()
    assert not any(c.strip() for c in next(csv.reader(io.StringIO(text[1]))))
    assert len(fp("QB").load()) == 12


def test_defenses_resolve_to_canonical_teams():
    """FantasyPros names defenses by full team name with an EMPTY team column."""
    rows = fp("DST").load()
    assert all(len(r.team) <= 4 for r in rows)
    assert all(r.name != r.team for r in rows)
    jags = next(r for r in rows if r.team == "JAX")
    assert jags.name == "Jaguars"


def test_unresolvable_defense_fails_loudly(tmp_path):
    target = tmp_path / "dst.csv"
    header = (FIXTURES / "fantasypros_dst.csv").read_text().splitlines()[0]
    target.write_text(header + '\n"Toronto Huskies","","2.0","1.0","0.5","0.5","0.1","0.0","18.0","300","7.0"\n')
    with pytest.raises(SlateSchemaError, match="could not resolve defense"):
        FantasyProsCsvAdapter(target, position="DST").load()


# ---------------------------------------------------------------------------
# Exports we refuse, and why
# ---------------------------------------------------------------------------

def test_kicker_export_is_refused():
    """DraftKings NFL Classic has no kicker slot."""
    with pytest.raises(SlateSchemaError, match="no kicker slot"):
        FantasyProsCsvAdapter(FIXTURES / "fantasypros_qb.csv", position="K")


def test_flex_export_is_refused():
    """FLEX duplicates RB+WR+TE; loading both double-counts every flex player."""
    with pytest.raises(SlateSchemaError, match="duplicates the RB, WR and TE"):
        FantasyProsCsvAdapter(FIXTURES / "fantasypros_rb.csv", position="FLX")


def test_unknown_position_is_refused():
    with pytest.raises(SlateSchemaError, match="unknown position"):
        FantasyProsCsvAdapter(FIXTURES / "fantasypros_rb.csv", position="LB")


def test_missing_file_fails_clearly(tmp_path):
    with pytest.raises(SlateSchemaError, match="does not exist"):
        FantasyProsCsvAdapter(tmp_path / "nope.csv", position="QB").load()


def test_empty_file_fails_clearly(tmp_path):
    target = tmp_path / "empty.csv"
    target.write_bytes(b"")
    with pytest.raises(SlateSchemaError, match="file is empty"):
        FantasyProsCsvAdapter(target, position="QB").load()


def test_header_only_file_fails_clearly(tmp_path):
    target = tmp_path / "headeronly.csv"
    target.write_text((FIXTURES / "fantasypros_qb.csv").read_text().splitlines()[0] + "\n")
    with pytest.raises(SlateSchemaError, match="no usable projection rows"):
        FantasyProsCsvAdapter(target, position="QB").load()


def test_short_nonblank_row_is_rejected(tmp_path):
    """A truncated row is corruption, not a spacer, and must not be skipped."""
    target = tmp_path / "short.csv"
    header = (FIXTURES / "fantasypros_te.csv").read_text().splitlines()[0]
    target.write_text(header + '\n"Some Player","KC","5.0"\n')
    with pytest.raises(SlateSchemaError, match="expected 7"):
        FantasyProsCsvAdapter(target, position="TE").load()


# ---------------------------------------------------------------------------
# Capture: a season average must not masquerade as a weekly projection
# ---------------------------------------------------------------------------

def test_season_averages_use_a_distinct_metric(store):
    """The whole point of the separate metric.

    Filing a season-long per-game average as `projection_dk_points` would put
    a number with no matchup, injury or game-script information into the same
    series a weekly slate projection occupies. Every downstream comparison
    would then be quietly meaningless.
    """
    result = ingest_projections(store, fp("RB"), captured_at="2026-08-17T18:00:00Z")
    assert result.rows == 12

    seasonal = store.as_of("2026-08-18T00:00:00Z", metric=SEASON_AVERAGE_METRIC)
    weekly = store.as_of("2026-08-18T00:00:00Z", metric="projection_dk_points")
    assert len(seasonal) == 12
    assert weekly == [], "season averages leaked into the weekly series"


def test_all_positions_capture_into_one_source(store):
    """Five files, one vendor: they merge in the store, not in the reader."""
    for position in ("QB", "RB", "WR", "TE", "DST"):
        ingest_projections(store, fp(position), captured_at="2026-08-17T18:00:00Z")
    rows = store.as_of("2026-08-18T00:00:00Z", metric=SEASON_AVERAGE_METRIC)
    assert len(rows) == 60
    assert {r.source for r in rows} == {"FANTASYPROS"}


def test_fantasypros_and_dff_stay_separate(store):
    """Two vendors disagreeing is signal; merging them destroys it."""
    from dfs_pipeline.adapters import ProjectionsCsvAdapter

    ingest_projections(store, fp("RB"), captured_at="2026-08-17T18:00:00Z")
    ingest_projections(
        store,
        ProjectionsCsvAdapter(FIXTURES / "projections_dff_real.csv", source_name="DFF"),
        captured_at="2026-08-17T18:00:00Z",
    )
    sources = {o.source for o in store.as_of("2026-08-18T00:00:00Z")}
    assert sources == {"FANTASYPROS", "DFF"}


def test_the_same_player_can_appear_in_two_position_files(store):
    """Regression: keying on name alone made one file overwrite the other.

    Connor Heyward, Riley Nowakowski and Max Bredeson each appear in BOTH the
    RB and TE exports with different projections -- FantasyPros is projecting
    two distinct usages and both numbers are real. Before position joined the
    key, ingesting RB then TE raised a UNIQUE violation; the store's
    append-only constraint caught what the adapter had missed.
    """
    ingest_projections(store, fp("RB"), captured_at="2026-08-17T18:00:00Z")
    ingest_projections(store, fp("TE"), captured_at="2026-08-17T18:00:00Z")
    rows = store.as_of("2026-08-18T00:00:00Z", metric=SEASON_AVERAGE_METRIC)
    assert len(rows) == 24, "one position file overwrote the other"


def test_subject_keys_carry_team_and_position():
    for row in fp("RB").load():
        assert row.subject_key.endswith("|RB")
        assert row.team in row.subject_key


def test_keys_are_unique_within_a_file():
    for position in ("QB", "RB", "WR", "TE", "DST"):
        keys = [r.subject_key for r in fp(position).load()]
        assert len(keys) == len(set(keys)), f"duplicate keys in {position}"


def test_blank_and_unparseable_numbers_become_zero(tmp_path):
    """FantasyPros leaves cells blank rather than writing 0."""
    header = (FIXTURES / "fantasypros_te.csv").read_text().splitlines()[0]
    target = tmp_path / "blanks.csv"
    target.write_text(header + '\n"Blank Guy","KC","","","","","0.0"\n'
                               '"Junk Guy","KC","n/a","x","","","0.0"\n')
    rows = FantasyProsCsvAdapter(target, position="TE").load()
    assert len(rows) == 2
    assert all(r.projection == 0.0 for r in rows)


def test_missing_directory_path_fails_clearly(tmp_path):
    with pytest.raises(SlateSchemaError, match="directory"):
        FantasyProsCsvAdapter(tmp_path, position="QB").load()


def test_row_with_only_a_blank_name_is_skipped(tmp_path):
    header = (FIXTURES / "fantasypros_te.csv").read_text().splitlines()[0]
    target = tmp_path / "blankname.csv"
    target.write_text(header + '\n"","KC","1","2","3","0","5.0"\n'
                               '"Real Guy","KC","4","40","0.5","0.1","8.0"\n')
    rows = FantasyProsCsvAdapter(target, position="TE").load()
    assert [r.name for r in rows] == ["Real Guy"]
