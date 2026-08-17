"""DraftKings NFL Classic scoring, as a single canonical definition.

Every scoring constant in this project lives here and nowhere else. Constants
duplicated across call sites drift apart, and a scoring engine that disagrees
with itself corrupts every downstream number -- projections, backtests,
realized results -- in a way that looks plausible.

Verification status
-------------------
**VERIFIED against DraftKings' published NFL Classic rules on 2026-08-17.**
Every constant below is transcribed from that page, and the test suite asserts
each component independently rather than only checking composite totals.

Still **UNVERIFIED**: that these values reproduce a *DraftKings-published
player total* for a real game. That requires a real contest box score to
compare against, and proving the arithmetic matches the published rules is
not the same as proving it matches DraftKings' own output. The distinction
matters and is not glossed over.

The continuous-yardage trap
---------------------------
DraftKings writes the yardage rules as "+1 Pt per 25 Passing Yards
(+0.04 Pts/Yard)". The parenthetical governs: scoring is **continuous, not
stepped**. 287 passing yards scores 11.48, not 11. An implementation using
integer division silently under-scores nearly every player on every slate --
plausible numbers, uniformly wrong, and very hard to notice.

Negative yardage scores negatively for the same reason: a quarterback with
-3 rushing yards loses 0.3 points.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "OffenseStats",
    "DefenseStats",
    "score_offense",
    "score_defense",
    "points_allowed_score",
    "POINTS_ALLOWED_TIERS",
]

# ---------------------------------------------------------------------------
# Offensive scoring constants
# ---------------------------------------------------------------------------

PASSING_TD: Final = 4.0
POINTS_PER_PASSING_YARD: Final = 0.04          # 1 point per 25 yards
BONUS_300_PASSING_YARDS: Final = 3.0
INTERCEPTION_THROWN: Final = -1.0

RUSHING_TD: Final = 6.0
POINTS_PER_RUSHING_YARD: Final = 0.1           # 1 point per 10 yards
BONUS_100_RUSHING_YARDS: Final = 3.0

RECEIVING_TD: Final = 6.0
POINTS_PER_RECEIVING_YARD: Final = 0.1         # 1 point per 10 yards
BONUS_100_RECEIVING_YARDS: Final = 3.0
RECEPTION: Final = 1.0                         # full PPR

RETURN_TD: Final = 6.0                         # punt / kickoff / FG return
OFFENSIVE_FUMBLE_RECOVERY_TD: Final = 6.0
FUMBLE_LOST: Final = -1.0
TWO_POINT_CONVERSION: Final = 2.0

#: Yardage thresholds at which the bonuses apply. DraftKings words these as
#: "300+" and "100+", so the comparison is inclusive.
PASSING_BONUS_THRESHOLD: Final = 300
RUSHING_BONUS_THRESHOLD: Final = 100
RECEIVING_BONUS_THRESHOLD: Final = 100

# ---------------------------------------------------------------------------
# Defense / special teams scoring constants
# ---------------------------------------------------------------------------

SACK: Final = 1.0                              # half-sacks score 0.5
DEF_INTERCEPTION: Final = 2.0
FUMBLE_RECOVERY: Final = 2.0
DEF_RETURN_TD: Final = 6.0                     # punt / kickoff / FG return
INTERCEPTION_RETURN_TD: Final = 6.0
FUMBLE_RECOVERY_TD: Final = 6.0
BLOCKED_KICK_RETURN_TD: Final = 6.0            # blocked punt or FG returned
SAFETY: Final = 2.0
BLOCKED_KICK: Final = 2.0
TWO_POINT_RETURN: Final = 2.0                  # 2pt conversion / XP return

#: Points-allowed tiers as (inclusive upper bound, points). ``None`` is the
#: open-ended top tier. Ordered, and evaluated first-match-wins.
POINTS_ALLOWED_TIERS: Final[tuple[tuple[int | None, float], ...]] = (
    (0, 10.0),
    (6, 7.0),
    (13, 4.0),
    (20, 1.0),
    (27, 0.0),
    (34, -1.0),
    (None, -4.0),
)

#: DraftKings reports fantasy points to two decimal places.
_DISPLAY_PRECISION: Final = 2


@dataclass(frozen=True, slots=True)
class OffenseStats:
    """A single player's offensive stat line for one game.

    Every field defaults to zero so a test or caller states only what is
    relevant, and a missing statistic can never be mistaken for a
    deliberately-set value.
    """

    passing_yards: float = 0.0
    passing_tds: int = 0
    interceptions_thrown: int = 0

    rushing_yards: float = 0.0
    rushing_tds: int = 0

    receptions: int = 0
    receiving_yards: float = 0.0
    receiving_tds: int = 0

    return_tds: int = 0
    offensive_fumble_recovery_tds: int = 0
    fumbles_lost: int = 0
    two_point_conversions: int = 0


@dataclass(frozen=True, slots=True)
class DefenseStats:
    """A defense/special-teams stat line for one game."""

    sacks: float = 0.0
    interceptions: int = 0
    fumble_recoveries: int = 0

    return_tds: int = 0
    interception_return_tds: int = 0
    fumble_recovery_tds: int = 0
    blocked_kick_return_tds: int = 0

    safeties: int = 0
    blocked_kicks: int = 0
    two_point_returns: int = 0

    #: Points surrendered *while the DST is on the field*. DraftKings excludes
    #: points the team's own offense gives up -- a pick-six thrown by your
    #: quarterback is not charged to your defense. Callers must supply the
    #: DST-attributable figure, not the opponent's final score.
    points_allowed: int = 0


def points_allowed_score(points_allowed: int) -> float:
    """Score the points-allowed tier for a defense.

    Raises on negative input: a negative points-allowed value is a data bug,
    and scoring it as the top tier would quietly reward the error.
    """
    if points_allowed < 0:
        raise ValueError(f"points_allowed cannot be negative, got {points_allowed}")
    for upper_bound, points in POINTS_ALLOWED_TIERS:
        if upper_bound is None or points_allowed <= upper_bound:
            return points
    # Unreachable while the tier table ends with an open-ended bound, which
    # test_tier_table_is_contiguous_and_ordered asserts. Kept as a guard
    # against a future edit that removes it.
    raise AssertionError(  # pragma: no cover
        "unreachable: tier table has no open-ended bound"
    )


def score_offense(stats: OffenseStats) -> float:
    """Total DraftKings points for an offensive stat line."""
    total = 0.0

    total += stats.passing_yards * POINTS_PER_PASSING_YARD
    total += stats.passing_tds * PASSING_TD
    total += stats.interceptions_thrown * INTERCEPTION_THROWN
    if stats.passing_yards >= PASSING_BONUS_THRESHOLD:
        total += BONUS_300_PASSING_YARDS

    total += stats.rushing_yards * POINTS_PER_RUSHING_YARD
    total += stats.rushing_tds * RUSHING_TD
    if stats.rushing_yards >= RUSHING_BONUS_THRESHOLD:
        total += BONUS_100_RUSHING_YARDS

    total += stats.receiving_yards * POINTS_PER_RECEIVING_YARD
    total += stats.receiving_tds * RECEIVING_TD
    total += stats.receptions * RECEPTION
    if stats.receiving_yards >= RECEIVING_BONUS_THRESHOLD:
        total += BONUS_100_RECEIVING_YARDS

    total += stats.return_tds * RETURN_TD
    total += stats.offensive_fumble_recovery_tds * OFFENSIVE_FUMBLE_RECOVERY_TD
    total += stats.fumbles_lost * FUMBLE_LOST
    total += stats.two_point_conversions * TWO_POINT_CONVERSION

    return round(total, _DISPLAY_PRECISION)


def score_defense(stats: DefenseStats) -> float:
    """Total DraftKings points for a defense/special-teams stat line."""
    total = 0.0

    total += stats.sacks * SACK
    total += stats.interceptions * DEF_INTERCEPTION
    total += stats.fumble_recoveries * FUMBLE_RECOVERY

    total += stats.return_tds * DEF_RETURN_TD
    total += stats.interception_return_tds * INTERCEPTION_RETURN_TD
    total += stats.fumble_recovery_tds * FUMBLE_RECOVERY_TD
    total += stats.blocked_kick_return_tds * BLOCKED_KICK_RETURN_TD

    total += stats.safeties * SAFETY
    total += stats.blocked_kicks * BLOCKED_KICK
    total += stats.two_point_returns * TWO_POINT_RETURN

    total += points_allowed_score(stats.points_allowed)

    return round(total, _DISPLAY_PRECISION)
