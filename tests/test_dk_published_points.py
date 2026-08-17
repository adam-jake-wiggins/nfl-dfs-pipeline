"""Verification that our scoring reproduces DraftKings' own published points.

The insight this rests on: **`AvgPointsPerGame` in a DraftKings salary export
is DraftKings' own fantasy-point arithmetic**, not a third party's. Reproducing
it from raw nflverse stat lines therefore checks our scoring against DK's
actual output, not merely against DK's published rules -- and it needs no
contest entry, no account, and no completed slate.

The fixture holds sixteen players' **raw per-game stat lines** alongside DK's
published average. The test recomputes from those stat lines rather than
replaying a stored answer, so a regression in `dfs_pipeline.scoring` fails here
even though the fixture never changes.

Two conventions this established, both discovered rather than assumed:

1. **DraftKings includes playoff games.** Restricting to the regular season
   left playoff participants off by a median of 0.229; including postseason
   games brought them to 0.019. The fixture carries every game a player
   appeared in, regular and post.
2. **DraftKings rounds to two decimals.** ~0.05 is therefore the best any
   correct implementation can do, and is the tolerance used below.

What this does NOT verify
-------------------------
An average over seventeen-plus games exercises the common path thoroughly --
yardage, touchdowns, receptions, the 300/100-yard bonuses, interceptions and
fumbles. It cannot exercise events that seldom occur: safeties, two-point
conversions, return touchdowns, offensive fumble-recovery touchdowns. Those
remain covered only by component tests against the published rules.

A single real contest box score would close that gap, and remains the reason
to keep the item open rather than declaring scoring finished.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

import pytest

from dfs_pipeline.results import offense_stats_from_row
from dfs_pipeline.scoring import score_offense

FIXTURE = Path(__file__).parent / "fixtures" / "dk_avg_points_verification.json"

#: DraftKings publishes AvgPointsPerGame to two decimals, so this is the floor
#: of achievable agreement rather than a tolerance chosen for convenience.
ROUNDING_TOLERANCE = 0.05


def players() -> list[dict]:
    return json.loads(FIXTURE.read_text())


def our_average(player: dict) -> float:
    return statistics.fmean(
        score_offense(offense_stats_from_row(game)) for game in player["games"]
    )


def test_the_fixture_covers_every_scoring_position():
    assert {p["position"] for p in players()} == {"QB", "RB", "WR", "TE"}
    assert len(players()) == 16


def test_the_fixture_carries_raw_stat_lines_not_stored_answers():
    """The test must recompute, or it verifies nothing about the scorer."""
    for player in players():
        assert player["games"], f"{player['name']} has no stat lines"
        assert "passing_yards" in player["games"][0]
        assert "dk_points" not in player["games"][0]


@pytest.mark.parametrize("player", players(), ids=lambda p: p["name"])
def test_our_scoring_reproduces_draftkings_published_average(player):
    """The verification itself, player by player."""
    published = player["dk_avg_points_per_game"]
    computed = our_average(player)
    assert computed == pytest.approx(published, abs=ROUNDING_TOLERANCE), (
        f"{player['name']}: DraftKings publishes {published}, we compute "
        f"{computed:.3f} over {len(player['games'])} games"
    )


def test_agreement_is_tight_in_aggregate():
    differences = [
        abs(our_average(p) - p["dk_avg_points_per_game"]) for p in players()
    ]
    assert statistics.median(differences) < 0.03
    assert max(differences) < ROUNDING_TOLERANCE


def test_no_systematic_bias_in_either_direction():
    """A scoring bug would push every player the same way.

    Random-looking scatter around zero is what rounding produces; a consistent
    sign would mean a term is wrong everywhere.
    """
    signed = [our_average(p) - p["dk_avg_points_per_game"] for p in players()]
    assert abs(statistics.fmean(signed)) < 0.02, "mean error should be ~0"
    assert any(x > 0 for x in signed) and any(x < 0 for x in signed)


def test_playoff_games_are_included_in_the_window():
    """DraftKings averages over regular AND postseason games.

    Discovered, not assumed: restricting to the regular season left playoff
    participants off by a median of 0.229, while including the postseason
    brought them to 0.019.
    """
    with_playoffs = [p for p in players() if len(p["games"]) > 17]
    assert with_playoffs, "fixture should include playoff participants"
    for player in with_playoffs:
        assert our_average(player) == pytest.approx(
            player["dk_avg_points_per_game"], abs=ROUNDING_TOLERANCE
        )


def test_dropping_the_playoff_games_makes_agreement_worse():
    """Proves the window matters, rather than asserting it.

    If truncating to seventeen games left agreement unchanged, the postseason
    finding would be an unsupported claim.
    """
    with_playoffs = [p for p in players() if len(p["games"]) > 17]
    assert with_playoffs

    degraded = 0
    for player in with_playoffs:
        full = abs(our_average(player) - player["dk_avg_points_per_game"])
        truncated = abs(
            statistics.fmean(
                score_offense(offense_stats_from_row(g))
                for g in player["games"][:17]
            )
            - player["dk_avg_points_per_game"]
        )
        if truncated > full:
            degraded += 1
    assert degraded == len(with_playoffs), (
        "truncating to the regular season should worsen every playoff player"
    )
