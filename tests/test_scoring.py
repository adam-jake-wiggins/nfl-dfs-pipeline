"""Tests for DraftKings NFL Classic scoring.

The handoff requires that the scoring fixture contain *full underlying stat
lines* paired with expected totals, so the tests prove every component rather
than only that one composite number happens to match. Each composite case
below therefore shows its arithmetic term by term in the docstring: if the
test fails, the comment says which term is wrong.

Every constant is checked individually first, because a composite total can
be right by cancellation -- two errors of equal magnitude and opposite sign
produce a passing test and a broken scorer.
"""

from __future__ import annotations

import pytest
from hypothesis import given, strategies as st

from dfs_pipeline.scoring import (
    DefenseStats,
    OffenseStats,
    POINTS_ALLOWED_TIERS,
    points_allowed_score,
    score_defense,
    score_offense,
)


# ---------------------------------------------------------------------------
# Offensive components, one at a time
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "stats, expected, why",
    [
        (OffenseStats(passing_tds=1), 4.0, "passing TD = +4"),
        (OffenseStats(passing_yards=25), 1.0, "25 passing yards = +1"),
        (OffenseStats(interceptions_thrown=1), -1.0, "interception thrown = -1"),
        (OffenseStats(rushing_tds=1), 6.0, "rushing TD = +6"),
        (OffenseStats(rushing_yards=10), 1.0, "10 rushing yards = +1"),
        (OffenseStats(receiving_tds=1), 6.0, "receiving TD = +6"),
        (OffenseStats(receiving_yards=10), 1.0, "10 receiving yards = +1"),
        (OffenseStats(receptions=1), 1.0, "reception = +1 (full PPR)"),
        (OffenseStats(return_tds=1), 6.0, "punt/kick/FG return TD = +6"),
        (OffenseStats(fumbles_lost=1), -1.0, "fumble lost = -1"),
        (OffenseStats(two_point_conversions=1), 2.0, "2pt conversion = +2"),
        (OffenseStats(offensive_fumble_recovery_tds=1), 6.0,
         "offensive fumble recovery TD = +6"),
    ],
)
def test_each_offensive_component(stats, expected, why):
    assert score_offense(stats) == expected, why


# ---------------------------------------------------------------------------
# The continuous-yardage rule -- the highest-value trap in the whole ruleset
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "yards, expected",
    [(287, 11.48), (25, 1.0), (24, 0.96), (1, 0.04), (0, 0.0), (349, 13.96 + 3)],
)
def test_passing_yards_score_continuously(yards, expected):
    """0.04 points per yard, NOT one point per completed 25-yard block.

    An implementation using integer division scores 287 yards as 11 instead
    of 11.48 -- under-scoring nearly every player on every slate by a small,
    plausible, uniformly-wrong amount.
    """
    assert score_offense(OffenseStats(passing_yards=yards)) == pytest.approx(expected)


@pytest.mark.parametrize(
    "yards, expected", [(87, 8.7), (10, 1.0), (9, 0.9), (1, 0.1), (0, 0.0)]
)
def test_rushing_yards_score_continuously(yards, expected):
    """Sub-100 values only, so this isolates continuity from the 100+ bonus."""
    assert score_offense(OffenseStats(rushing_yards=yards)) == pytest.approx(expected)


def test_rushing_yardage_and_bonus_combine():
    """137 yards crosses the threshold: 13.70 continuous + 3.00 bonus."""
    assert score_offense(OffenseStats(rushing_yards=137)) == pytest.approx(16.7)


@pytest.mark.parametrize("yards, expected", [(83, 8.3), (10, 1.0), (7, 0.7)])
def test_receiving_yards_score_continuously(yards, expected):
    assert score_offense(OffenseStats(receiving_yards=yards)) == pytest.approx(expected)


def test_negative_rushing_yards_score_negatively():
    """A sacked or scrambling quarterback can finish with negative yardage."""
    assert score_offense(OffenseStats(rushing_yards=-3)) == pytest.approx(-0.3)


# ---------------------------------------------------------------------------
# Yardage bonuses: thresholds are inclusive
# ---------------------------------------------------------------------------

def test_300_passing_bonus_is_inclusive_at_300():
    """DraftKings writes "300+", so exactly 300 earns it."""
    at = score_offense(OffenseStats(passing_yards=300))
    just_under = score_offense(OffenseStats(passing_yards=299))
    assert at == pytest.approx(12.0 + 3.0)
    assert just_under == pytest.approx(11.96)
    assert at - just_under == pytest.approx(3.04), "bonus should appear at 300"


def test_100_rushing_bonus_is_inclusive_at_100():
    at = score_offense(OffenseStats(rushing_yards=100))
    just_under = score_offense(OffenseStats(rushing_yards=99))
    assert at == pytest.approx(10.0 + 3.0)
    assert at - just_under == pytest.approx(3.1)


def test_100_receiving_bonus_is_inclusive_at_100():
    at = score_offense(OffenseStats(receiving_yards=100))
    just_under = score_offense(OffenseStats(receiving_yards=99))
    assert at == pytest.approx(10.0 + 3.0)
    assert at - just_under == pytest.approx(3.1)


def test_rushing_and_receiving_bonuses_both_apply():
    """A player crossing 100 in both directions earns both bonuses."""
    stats = OffenseStats(rushing_yards=100, receiving_yards=100, receptions=8)
    # 10.0 rush + 3 bonus + 10.0 rec + 3 bonus + 8 receptions
    assert score_offense(stats) == pytest.approx(34.0)


# ---------------------------------------------------------------------------
# Composite offensive lines, with the arithmetic spelled out
# ---------------------------------------------------------------------------

def test_quarterback_line():
    """333 pass yds, 3 pass TD, 1 INT, 28 rush yds, 1 rush TD.

        333 * 0.04  = 13.32
        3 * 4       = 12.00
        1 * -1      = -1.00
        300+ bonus  = +3.00
        28 * 0.1    =  2.80
        1 * 6       =  6.00
                      ------
                      36.12
    """
    stats = OffenseStats(
        passing_yards=333, passing_tds=3, interceptions_thrown=1,
        rushing_yards=28, rushing_tds=1,
    )
    assert score_offense(stats) == pytest.approx(36.12)


def test_running_back_line():
    """21 car, 118 rush yds, 1 rush TD, 5 rec, 42 rec yds, 1 fumble lost.

        118 * 0.1   = 11.80
        1 * 6       =  6.00
        100+ bonus  = +3.00
        5 * 1       =  5.00
        42 * 0.1    =  4.20
        1 * -1      = -1.00
                      ------
                      29.00
    """
    stats = OffenseStats(
        rushing_yards=118, rushing_tds=1,
        receptions=5, receiving_yards=42, fumbles_lost=1,
    )
    assert score_offense(stats) == pytest.approx(29.0)


def test_wide_receiver_line():
    """9 rec, 147 rec yds, 2 rec TD, 1 two-point conversion.

        9 * 1       =  9.00
        147 * 0.1   = 14.70
        2 * 6       = 12.00
        100+ bonus  = +3.00
        1 * 2       =  2.00
                      ------
                      40.70
    """
    stats = OffenseStats(
        receptions=9, receiving_yards=147, receiving_tds=2, two_point_conversions=1
    )
    assert score_offense(stats) == pytest.approx(40.7)


def test_a_zero_line_scores_zero():
    assert score_offense(OffenseStats()) == 0.0


def test_a_line_can_score_negative():
    """Two turnovers and no production is a real, if unhappy, outcome."""
    assert score_offense(
        OffenseStats(interceptions_thrown=2, fumbles_lost=1)
    ) == pytest.approx(-3.0)


# ---------------------------------------------------------------------------
# Defensive components
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "stats, expected, why",
    [
        (DefenseStats(sacks=1, points_allowed=24), 1.0, "sack = +1"),
        (DefenseStats(interceptions=1, points_allowed=24), 2.0, "interception = +2"),
        (DefenseStats(fumble_recoveries=1, points_allowed=24), 2.0, "fumble recovery = +2"),
        (DefenseStats(return_tds=1, points_allowed=24), 6.0, "return TD = +6"),
        (DefenseStats(interception_return_tds=1, points_allowed=24), 6.0, "pick six = +6"),
        (DefenseStats(fumble_recovery_tds=1, points_allowed=24), 6.0, "fumble return TD = +6"),
        (DefenseStats(blocked_kick_return_tds=1, points_allowed=24), 6.0, "blocked kick TD = +6"),
        (DefenseStats(safeties=1, points_allowed=24), 2.0, "safety = +2"),
        (DefenseStats(blocked_kicks=1, points_allowed=24), 2.0, "blocked kick = +2"),
        (DefenseStats(two_point_returns=1, points_allowed=24), 2.0, "2pt return = +2"),
    ],
)
def test_each_defensive_component(stats, expected, why):
    """Each case uses 21-27 points allowed, which scores 0, isolating the term."""
    assert score_defense(stats) == expected, why


def test_half_sacks_score_half_a_point():
    """Shared sacks are credited as 0.5 in official statistics."""
    assert score_defense(DefenseStats(sacks=2.5, points_allowed=24)) == 2.5


# ---------------------------------------------------------------------------
# Points-allowed tiers, including every boundary
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "points_allowed, expected",
    [
        (0, 10.0),
        (1, 7.0), (6, 7.0),
        (7, 4.0), (13, 4.0),
        (14, 1.0), (20, 1.0),
        (21, 0.0), (27, 0.0),
        (28, -1.0), (34, -1.0),
        (35, -4.0), (59, -4.0),
    ],
)
def test_points_allowed_tiers_at_every_boundary(points_allowed, expected):
    """Both edges of every band, where off-by-one errors live."""
    assert points_allowed_score(points_allowed) == expected


def test_negative_points_allowed_is_rejected():
    """Scoring it as the shutout tier would quietly reward a data bug."""
    with pytest.raises(ValueError, match="cannot be negative"):
        points_allowed_score(-1)


def test_tier_table_is_contiguous_and_ordered():
    """Guards the table itself: no gaps, no overlaps, open-ended at the top."""
    bounds = [b for b, _ in POINTS_ALLOWED_TIERS]
    assert bounds[-1] is None, "top tier must be open-ended"
    finite = [b for b in bounds if b is not None]
    assert finite == sorted(finite), "tiers must ascend"
    assert len(set(finite)) == len(finite), "duplicate tier bounds"


@given(points_allowed=st.integers(min_value=0, max_value=100))
def test_every_points_allowed_value_scores(points_allowed):
    """No input in the plausible range may fall through the table."""
    assert isinstance(points_allowed_score(points_allowed), float)


# ---------------------------------------------------------------------------
# Composite defensive lines
# ---------------------------------------------------------------------------

def test_shutout_defense():
    """4 sacks, 2 INT, 1 fumble recovery, 0 points allowed.

        4 * 1       =  4.00
        2 * 2       =  4.00
        1 * 2       =  2.00
        shutout     = 10.00
                      ------
                      20.00
    """
    stats = DefenseStats(
        sacks=4, interceptions=2, fumble_recoveries=1, points_allowed=0
    )
    assert score_defense(stats) == pytest.approx(20.0)


def test_blowout_loss_defense():
    """1 sack, 41 points allowed -- the worst tier.

        1 * 1       =  1.00
        35+ allowed = -4.00
                      ------
                      -3.00
    """
    assert score_defense(
        DefenseStats(sacks=1, points_allowed=41)
    ) == pytest.approx(-3.0)


def test_defense_with_a_pick_six():
    """3 sacks, 1 INT returned for a TD, 10 points allowed.

    The interception counts AND its return TD counts -- they are separate
    scoring events, not alternatives.

        3 * 1        =  3.00
        1 INT * 2    =  2.00
        pick six * 6 =  6.00
        7-13 allowed =  4.00
                       ------
                       15.00
    """
    stats = DefenseStats(
        sacks=3, interceptions=1, interception_return_tds=1, points_allowed=10
    )
    assert score_defense(stats) == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

@given(
    receptions=st.integers(min_value=0, max_value=20),
    yards=st.integers(min_value=-20, max_value=250),
    tds=st.integers(min_value=0, max_value=5),
)
def test_offense_scoring_is_monotone_in_touchdowns(receptions, yards, tds):
    """Adding a touchdown can never reduce a score."""
    base = OffenseStats(receptions=receptions, receiving_yards=yards, receiving_tds=tds)
    more = OffenseStats(
        receptions=receptions, receiving_yards=yards, receiving_tds=tds + 1
    )
    assert score_offense(more) > score_offense(base)


@given(
    passing_yards=st.integers(min_value=0, max_value=600),
    rushing_yards=st.integers(min_value=-30, max_value=300),
)
def test_scoring_is_deterministic(passing_yards, rushing_yards):
    """Same input, same output -- required for reproducible backtests."""
    stats = OffenseStats(passing_yards=passing_yards, rushing_yards=rushing_yards)
    assert score_offense(stats) == score_offense(stats)


@given(points_allowed=st.integers(min_value=0, max_value=60))
def test_points_allowed_score_never_increases_with_more_points(points_allowed):
    """Surrendering more points can never help the defense."""
    if points_allowed >= 60:
        return
    assert points_allowed_score(points_allowed + 1) <= points_allowed_score(
        points_allowed
    )


def test_results_round_to_two_decimals():
    """DraftKings reports to two decimal places; float noise must not leak."""
    score = score_offense(OffenseStats(rushing_yards=87))
    assert score == 8.7
    assert len(str(score).split(".")[-1]) <= 2
