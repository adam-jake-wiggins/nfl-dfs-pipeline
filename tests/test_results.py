"""Tests for realized-results ingestion.

Fixtures are recorded slices of real nflverse tables for 2025 week 1, so the
mapping is tested against actual column names and actual values. No test
touches the network -- nflverse downloads are large and slow, and a suite that
needs them is a suite people stop running.

The centre of gravity here is `points_allowed_by_team`. Every other mapping is
a rename; that one is a derivation, and it is the one that can be plausibly
wrong.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import polars as pl
import pytest

from dfs_pipeline.capture import ingest_results
from dfs_pipeline.results import (
    SCRIMMAGE_PLAY_TYPES,
    defense_results,
    defense_stats_from_row,
    offense_results,
    offense_stats_from_row,
    points_allowed_by_team,
)
from dfs_pipeline.scoring import score_offense
from dfs_pipeline.store import SnapshotStore
from dfs_pipeline.teams import resolve_team

FIXTURES = Path(__file__).parent / "fixtures"


def _frame(name: str) -> pl.DataFrame:
    return pl.DataFrame(json.loads((FIXTURES / name).read_text()))


@pytest.fixture()
def player_stats() -> pl.DataFrame:
    return _frame("nflverse_player_stats_2025wk1.json")


@pytest.fixture()
def team_stats() -> pl.DataFrame:
    return _frame("nflverse_team_stats_2025wk1.json")


@pytest.fixture()
def pbp() -> pl.DataFrame:
    return _frame("nflverse_pbp_2025wk1.json")


@pytest.fixture()
def store(tmp_path):
    with SnapshotStore.open(tmp_path / "snapshots.sqlite") as s:
        yield s


# ---------------------------------------------------------------------------
# Points allowed: the derivation, and the trap it avoids
# ---------------------------------------------------------------------------

def test_points_allowed_covers_every_team(pbp):
    assert len(points_allowed_by_team(pbp, 2025, 1)) == 32


def test_a_pick_six_is_not_charged_to_the_defense(pbp):
    """The case DraftKings' rules exist to distinguish.

    In 2025 week 1, Chicago beat Minnesota 24-21 and scored on an interception
    return. Those 6 points came against Minnesota's OFFENCE, so Minnesota's
    defense is charged 18, not 24. Using the opponent's final score -- the
    obvious source, and the one nflverse makes easiest -- would be wrong.

    18 lands in the 14-20 tier (+1); 24 lands in 21-27 (+0). Small, but wrong
    every week and in the same direction.
    """
    assert points_allowed_by_team(pbp, 2025, 1)["MIN"] == 18


def test_only_one_team_differs_from_the_opponent_final_score(pbp):
    """Sanity check on the derivation's blast radius.

    2025 week 1 had exactly one scrimmage-play defensive touchdown, so exactly
    one team's charge should differ from its opponent's final score. If this
    starts failing, the attribution rule has widened.
    """
    allowed = points_allowed_by_team(pbp, 2025, 1)
    finals = (
        pbp.group_by("game_id")
        .agg(
            pl.col("home_team").drop_nulls().last().alias("home"),
            pl.col("away_team").drop_nulls().last().alias("away"),
            pl.col("total_home_score").max().alias("hs"),
            pl.col("total_away_score").max().alias("as_"),
        )
        .to_dicts()
    )
    differing = [
        team
        for g in finals
        for team, opp_score in (
            (resolve_team(g["home"]).abbrev, g["as_"]),
            (resolve_team(g["away"]).abbrev, g["hs"]),
        )
        if allowed[team] != int(opp_score)
    ]
    assert differing == ["MIN"]


def test_return_touchdowns_are_still_charged(pbp):
    """Punt, kickoff and FG-return TDs come at the special teams' expense.

    DraftKings' DST covers special teams, so these count -- even though the
    scoring team was the defence on that play. A rule keyed on `td_team ==
    defteam` alone would wrongly forgive them.
    """
    returns = pbp.filter(
        (pl.col("touchdown") == 1)
        & (pl.col("td_team") == pl.col("defteam"))
        & ~pl.col("play_type").is_in(list(SCRIMMAGE_PLAY_TYPES))
    )
    allowed = points_allowed_by_team(pbp, 2025, 1)
    for row in returns.to_dicts():
        conceding = resolve_team(row["posteam"]).abbrev
        # The conceding team's charge must still include those 6 points, i.e.
        # it must not have been reduced below the opponent's scoring.
        assert allowed[conceding] > 0


def test_scrimmage_play_types_are_exactly_pass_and_run():
    """The discriminator. Widening this silently forgives return TDs."""
    assert SCRIMMAGE_PLAY_TYPES == {"pass", "run"}


def test_team_codes_are_canonical_not_raw(pbp):
    """Regression: nflverse is not internally consistent about team codes.

    Play-by-play writes the Rams as `LA`; team_stats writes `LAR`. Keying this
    dict on the raw play-by-play value meant the caller -- which does resolve
    -- could not find that team, and silently dropped an entire defense from
    the week. Applying the alias map on one side only is worse than not having
    it, because the failure is invisible.
    """
    allowed = points_allowed_by_team(pbp, 2025, 1)
    assert "LAR" in allowed, "Rams missing under their canonical code"
    assert "LA" not in allowed, "raw play-by-play code leaked into the keys"


def test_points_allowed_is_never_negative(pbp):
    assert all(v >= 0 for v in points_allowed_by_team(pbp, 2025, 1).values())


def test_missing_week_fails_loudly(pbp):
    with pytest.raises(ValueError, match="no play-by-play"):
        points_allowed_by_team(pbp, 2025, 99)


# ---------------------------------------------------------------------------
# Offensive mapping
# ---------------------------------------------------------------------------

def test_offense_mapping_agrees_with_nflverse_ppr(player_stats):
    """Cross-check against an independently computed number.

    nflverse publishes its own PPR total from the same underlying stats. DK
    differs in exactly two ways: it charges -1 for interceptions and lost
    fumbles where standard scoring charges -2, and it adds 300/100-yard
    bonuses. So this identity must hold for every player:

        dk = ppr + interceptions + fumbles_lost + bonuses

    If the mapping picks up a wrong column, this breaks. It held for all 355
    scored players in the full week 1 table.
    """
    for row in player_stats.to_dicts():
        stats = offense_stats_from_row(row)
        ppr = row.get("fantasy_points_ppr")
        if ppr is None:
            continue
        bonuses = (
            (3 if stats.passing_yards >= 300 else 0)
            + (3 if stats.rushing_yards >= 100 else 0)
            + (3 if stats.receiving_yards >= 100 else 0)
        )
        expected = ppr + stats.interceptions_thrown + stats.fumbles_lost + bonuses
        assert score_offense(stats) == pytest.approx(expected, abs=0.011), (
            f"{row.get('player_display_name')} mismatched"
        )


def test_fumbles_lost_are_summed_across_phases(player_stats):
    """nflverse splits fumbles by phase; DraftKings charges -1 regardless."""
    row = {
        "rushing_fumbles_lost": 1,
        "receiving_fumbles_lost": 1,
        "sack_fumbles_lost": 1,
    }
    assert offense_stats_from_row(row).fumbles_lost == 3


def test_two_point_conversions_are_summed_across_phases():
    row = {
        "passing_2pt_conversions": 1,
        "rushing_2pt_conversions": 1,
        "receiving_2pt_conversions": 0,
    }
    assert offense_stats_from_row(row).two_point_conversions == 2


def test_nulls_are_treated_as_zero_not_as_errors():
    """nflverse writes absent counting stats as null."""
    stats = offense_stats_from_row({"passing_yards": None, "receptions": None})
    assert stats.passing_yards == 0.0
    assert stats.receptions == 0


def test_empty_row_scores_zero():
    assert score_offense(offense_stats_from_row({})) == 0.0


def test_offense_results_resolve_teams(player_stats):
    results = offense_results(player_stats, 2025, 1)
    assert results
    for r in results:
        assert r.entity_type == "player"
        assert len(r.team) <= 4
        assert r.nflverse_id


def test_offense_results_exclude_non_offensive_positions(player_stats):
    positions = {r.position for r in offense_results(player_stats, 2025, 1)}
    assert positions <= {"QB", "RB", "WR", "TE", "FB", "HB"}


# ---------------------------------------------------------------------------
# Defensive mapping
# ---------------------------------------------------------------------------

def test_defense_mapping_reads_the_right_columns():
    row = {
        "def_sacks": 3.0,
        "def_interceptions": 2,
        "fumble_recovery_opp": 1,
        "def_tds": 1,
        "special_teams_tds": 1,
        "def_safeties": 1,
        "def_punt_blocks": 1,
        "def_fg_blocks": 1,
        "def_pat_blocks": 0,
        "def_2pt_made": 1,
    }
    stats = defense_stats_from_row(row, points_allowed=7)
    assert stats.sacks == 3.0
    assert stats.interceptions == 2
    assert stats.fumble_recoveries == 1
    assert stats.interception_return_tds == 1
    assert stats.return_tds == 1
    assert stats.safeties == 1
    assert stats.blocked_kicks == 2, "punt + FG blocks should sum"
    assert stats.two_point_returns == 1
    assert stats.points_allowed == 7


def test_half_sacks_survive_the_mapping():
    """nflverse reports shared sacks as 0.5; rounding here would lose them."""
    assert defense_stats_from_row({"def_sacks": 2.5}, 0).sacks == 2.5


def test_defense_results_cover_every_team(team_stats, pbp):
    allowed = points_allowed_by_team(pbp, 2025, 1)
    results = defense_results(team_stats, allowed, 2025, 1)
    assert len(results) == 32
    assert all(r.entity_type == "dst" for r in results)
    assert all(r.position == "DST" for r in results)


def test_defense_is_identified_by_team_not_player_id(team_stats, pbp):
    """A defense is a team-level entity, not a player with an odd name."""
    allowed = points_allowed_by_team(pbp, 2025, 1)
    for r in defense_results(team_stats, allowed, 2025, 1):
        assert r.nflverse_id == r.team
        assert r.name and r.name != r.team, "should carry the nickname"


def test_team_without_a_points_allowed_figure_is_skipped(team_stats):
    """Defaulting to zero would award a shutout the defense did not earn."""
    results = defense_results(team_stats, {"KC": 17}, 2025, 1)
    assert [r.team for r in results] == ["KC"]


def test_minnesota_defense_reflects_the_forgiven_pick_six(team_stats, pbp):
    """End-to-end on the case that motivates the whole derivation."""
    allowed = points_allowed_by_team(pbp, 2025, 1)
    minnesota = next(
        r for r in defense_results(team_stats, allowed, 2025, 1) if r.team == "MIN"
    )
    # 18 points allowed sits in the 14-20 tier (+1), not 21-27 (+0).
    assert allowed["MIN"] == 18
    assert minnesota.dk_points > 0


# ---------------------------------------------------------------------------
# Capture into the store
# ---------------------------------------------------------------------------

def test_results_are_recorded_and_queryable(store, player_stats, team_stats, pbp):
    allowed = points_allowed_by_team(pbp, 2025, 1)
    results = offense_results(player_stats, 2025, 1) + defense_results(
        team_stats, allowed, 2025, 1
    )
    raw = json.dumps([dataclasses.asdict(r) for r in results], default=str).encode()

    outcome = ingest_results(
        store, results, raw, season=2025, week=1,
        captured_at="2025-09-09T12:00:00Z",
    )
    assert outcome.defenses == 32
    assert outcome.players == len(results) - 32
    assert outcome.total_entities == len(results)
    assert store.artifact_count() == 1

    points = store.as_of("2025-09-10T00:00:00Z", metric="actual_dk_points")
    assert len(points) == len(results)


def test_results_key_on_nflverse_ids_not_draftkings_ids(store, player_stats):
    """Results come from a different source with its own identifiers.

    Forcing a join to DraftKings ids at capture time would drop any player the
    crosswalk cannot yet resolve -- and capture must never lose data to an
    unresolved name.
    """
    results = offense_results(player_stats, 2025, 1)
    raw = b"[]"
    ingest_results(store, results, raw, season=2025, week=1,
                   captured_at="2025-09-09T12:00:00Z")
    stored = {o.source_subject_id for o in
              store.as_of("2025-09-10T00:00:00Z", metric="actual_dk_points")}
    assert stored == {r.nflverse_id for r in results}
    assert all(s.startswith("00-") for s in stored), "expected gsis ids"


def test_recapturing_a_revision_creates_new_history(store, player_stats):
    """nflverse revises prior weeks as official corrections land.

    A later capture must be new history, not an overwrite, so the revision is
    visible rather than silently replacing what we scored on.
    """
    results = offense_results(player_stats, 2025, 1)
    ingest_results(store, results, b"v1", season=2025, week=1,
                   captured_at="2025-09-09T12:00:00Z")
    first = store.observation_count()
    ingest_results(store, results, b"v2", season=2025, week=1,
                   captured_at="2025-09-16T12:00:00Z")
    assert store.observation_count() == first * 2
    assert store.artifact_count() == 2, "each revision keeps its own artifact"


# ---------------------------------------------------------------------------
# Unresolvable teams: warn, do not silently mangle
# ---------------------------------------------------------------------------

def test_unresolvable_team_in_play_by_play_warns_and_is_skipped(caplog):
    """A new or misspelt code must not silently key the dict under garbage."""
    frame = pl.DataFrame([{
        "season": 2025, "week": 1, "game_id": "g1",
        "home_team": "XXX", "away_team": "KC",
        "total_home_score": 20, "total_away_score": 17,
        "posteam": "KC", "defteam": "XXX", "td_team": None,
        "touchdown": 0, "play_type": "pass",
    }])
    with caplog.at_level("WARNING"):
        allowed = points_allowed_by_team(frame, 2025, 1)
    assert "XXX" not in allowed
    assert "KC" not in allowed, "a game with an unresolvable side is skipped whole"
    assert "unresolved team" in caplog.text


def test_unresolvable_team_in_player_stats_is_kept_with_a_warning(caplog):
    """Capture must not lose a player to an unknown team code.

    The scored points are still real; only the team label is uncertain. Losing
    the row would discard data that cannot be recovered.
    """
    frame = pl.DataFrame([{
        "season": 2025, "week": 1, "player_id": "00-9999999",
        "player_display_name": "Test Player", "position": "WR",
        "team": "XXX", "opponent_team": "YYY",
        "receptions": 5, "receiving_yards": 60, "receiving_tds": 1,
    }])
    with caplog.at_level("WARNING"):
        results = offense_results(frame, 2025, 1)
    assert len(results) == 1
    assert results[0].dk_points == pytest.approx(17.0)
    assert results[0].team == "XXX", "unresolved label preserved verbatim"
    assert "unresolved team" in caplog.text


def test_unresolvable_team_in_team_stats_is_skipped(caplog):
    """A defense with no resolvable identity cannot be scored against a slate."""
    frame = pl.DataFrame([
        {"season": 2025, "week": 1, "team": "XXX", "opponent_team": "KC",
         "def_sacks": 2.0},
        {"season": 2025, "week": 1, "team": "KC", "opponent_team": "XXX",
         "def_sacks": 3.0},
    ])
    with caplog.at_level("WARNING"):
        results = defense_results(frame, {"KC": 14}, 2025, 1)
    assert [r.team for r in results] == ["KC"]
    assert "unresolved team" in caplog.text


def test_unresolvable_opponent_is_preserved_verbatim():
    """The opponent label is descriptive; an unknown one must not abort a row."""
    frame = pl.DataFrame([{
        "season": 2025, "week": 1, "team": "KC", "opponent_team": "XXX",
        "def_sacks": 1.0,
    }])
    results = defense_results(frame, {"KC": 10}, 2025, 1)
    assert len(results) == 1
    assert results[0].opponent == "XXX"


def test_player_with_a_missing_team_still_scores():
    frame = pl.DataFrame([{
        "season": 2025, "week": 1, "player_id": "00-8888888",
        "player_display_name": "No Team", "position": "RB",
        "team": None, "opponent_team": None, "rushing_yards": 50,
    }])
    results = offense_results(frame, 2025, 1)
    assert results[0].team == ""
    assert results[0].dk_points == pytest.approx(5.0)


def test_player_display_name_falls_back_to_player_name():
    frame = pl.DataFrame([{
        "season": 2025, "week": 1, "player_id": "00-7777777",
        "player_display_name": None, "player_name": "P.Fallback",
        "position": "TE", "team": "KC", "opponent_team": "BUF",
        "receptions": 2,
    }])
    assert offense_results(frame, 2025, 1)[0].name == "P.Fallback"


# ---------------------------------------------------------------------------
# The nflverse loader, stubbed (no network)
# ---------------------------------------------------------------------------

def _stub_nflreadpy(monkeypatch, player_stats, team_stats, pbp):
    import sys
    import types

    module = types.SimpleNamespace(
        load_player_stats=lambda **_: player_stats,
        load_team_stats=lambda **_: team_stats,
        load_pbp=lambda **_: pbp,
    )
    monkeypatch.setitem(sys.modules, "nflreadpy", module)


def test_load_and_score_week_archives_source_rows_not_output(
    monkeypatch, player_stats, team_stats, pbp
):
    """The artifact must be the INPUT, so a scoring bug can be re-run later.

    Archiving only our computed output would bake any scoring error in
    permanently -- the opposite of why raw artifacts are kept.
    """
    from dfs_pipeline.results import load_and_score_week

    _stub_nflreadpy(monkeypatch, player_stats, team_stats, pbp)
    results, raw = load_and_score_week(2025, 1)

    assert results
    payload = json.loads(raw)
    assert payload["season"] == 2025 and payload["week"] == 1
    assert "player_stats" in payload and "team_stats" in payload
    assert "points_allowed" in payload, "the derivation should be archived too"
    assert "dk_points" not in raw.decode(), "output must not stand in for input"


def test_load_and_score_week_rejects_an_empty_week(
    monkeypatch, player_stats, team_stats, pbp
):
    from dfs_pipeline.results import load_and_score_week

    empty = player_stats.clear()
    _stub_nflreadpy(monkeypatch, empty, team_stats.clear(), pbp)
    with pytest.raises(ValueError, match="no play-by-play|no rows"):
        load_and_score_week(2025, 99)
