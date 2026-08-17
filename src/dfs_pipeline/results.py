"""Realized results: nflverse statistics scored at DraftKings Classic rules.

This closes the loop from "what we knew" to "what actually happened". Nothing
here invents a scoring rule -- every constant comes from
:mod:`dfs_pipeline.scoring`, which was transcribed from DraftKings' published
page. This module's only job is mapping nflverse's column names onto that
vocabulary correctly.

The points-allowed trap
-----------------------
DraftKings charges a defense only for points surrendered **while the DST was
on the field**. The obvious source -- the opponent's final score -- is
therefore *wrong*, and wrong in a way that produces plausible numbers.

nflverse has no points-allowed column at any level, so it must be derived.
The rule, from DraftKings' published notes: a touchdown scored by the
opponent's *defense against our offense* (a pick-six, a fumble returned on a
scrimmage play) is not charged to our DST. Everything else the opponent scores
is -- including punt, kickoff and field-goal return touchdowns, because those
come at the expense of our special teams, and DST covers special teams.

That distinction cannot be made from ``td_team`` alone. In 2025 weeks 1-4,
18 touchdowns were scored by the team on defense for that play; only 8 were
scrimmage-play defensive scores. The other 10 were return touchdowns, which
*do* count. A rule keyed on "the defence scored" would wrongly forgive them.

One documented ambiguity: the extra point following an opponent's pick-six.
DraftKings lists "Extra-points" as points allowed without qualification, so it
is counted here even though the touchdown it follows is not. Stated because it
is a judgement call, not a derivation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import polars as pl

from dfs_pipeline.scoring import (
    DefenseStats,
    OffenseStats,
    score_defense,
    score_offense,
)
from dfs_pipeline.teams import UnknownTeam, resolve_team

__all__ = [
    "PlayerResult",
    "SCRIMMAGE_PLAY_TYPES",
    "offense_results",
    "defense_results",
    "points_allowed_by_team",
    "offense_stats_from_row",
    "defense_stats_from_row",
    "load_and_score_week",
]

log = logging.getLogger("dfs_pipeline.results")

#: Play types on which a defensive touchdown is scored *against the offense*
#: and is therefore NOT charged to that offense's own defense.
SCRIMMAGE_PLAY_TYPES = frozenset({"pass", "run"})

#: Positions scored with offensive rules. Everyone else on a roster either
#: does not accrue DraftKings points or is covered by the team defense.
OFFENSIVE_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "FB", "HB"})

_TOUCHDOWN_POINTS = 6


@dataclass(frozen=True, slots=True)
class PlayerResult:
    """One scored entity for one game."""

    season: int
    week: int
    entity_type: str          #: 'player' or 'dst'
    nflverse_id: str          #: gsis id for players, team abbreviation for a DST
    name: str
    team: str
    opponent: str
    position: str
    dk_points: float


def _num(value) -> float:
    """nflverse writes absent counting stats as null; absent means zero here."""
    return 0.0 if value is None else float(value)


def offense_stats_from_row(row: dict) -> OffenseStats:
    """Map one nflverse ``player_stats`` row onto :class:`OffenseStats`.

    Fumbles lost are summed across rushing, receiving and sack fumbles --
    DraftKings charges -1 for a lost fumble regardless of how it happened, and
    nflverse splits them by phase.
    """
    return OffenseStats(
        passing_yards=_num(row.get("passing_yards")),
        passing_tds=int(_num(row.get("passing_tds"))),
        interceptions_thrown=int(_num(row.get("passing_interceptions"))),
        rushing_yards=_num(row.get("rushing_yards")),
        rushing_tds=int(_num(row.get("rushing_tds"))),
        receptions=int(_num(row.get("receptions"))),
        receiving_yards=_num(row.get("receiving_yards")),
        receiving_tds=int(_num(row.get("receiving_tds"))),
        return_tds=int(_num(row.get("special_teams_tds"))),
        offensive_fumble_recovery_tds=int(_num(row.get("fumble_recovery_tds"))),
        fumbles_lost=int(
            _num(row.get("rushing_fumbles_lost"))
            + _num(row.get("receiving_fumbles_lost"))
            + _num(row.get("sack_fumbles_lost"))
        ),
        two_point_conversions=int(
            _num(row.get("passing_2pt_conversions"))
            + _num(row.get("rushing_2pt_conversions"))
            + _num(row.get("receiving_2pt_conversions"))
        ),
    )


def defense_stats_from_row(row: dict, points_allowed: int) -> DefenseStats:
    """Map one nflverse ``team_stats`` row onto :class:`DefenseStats`.

    ``points_allowed`` must be the DST-attributable figure from
    :func:`points_allowed_by_team`, not the opponent's final score.

    ``def_tds`` is nflverse's combined count of defensive touchdowns
    (interception returns and fumble returns). DraftKings scores both at +6,
    so they are carried in one field rather than split on a guess -- inventing
    a split we cannot derive would be worse than declining to.
    """
    return DefenseStats(
        sacks=_num(row.get("def_sacks")),
        interceptions=int(_num(row.get("def_interceptions"))),
        fumble_recoveries=int(_num(row.get("fumble_recovery_opp"))),
        return_tds=int(_num(row.get("special_teams_tds"))),
        interception_return_tds=int(_num(row.get("def_tds"))),
        safeties=int(_num(row.get("def_safeties"))),
        blocked_kicks=int(
            _num(row.get("def_punt_blocks"))
            + _num(row.get("def_fg_blocks"))
            + _num(row.get("def_pat_blocks"))
        ),
        two_point_returns=int(_num(row.get("def_2pt_made"))),
        points_allowed=points_allowed,
    )


def points_allowed_by_team(pbp: pl.DataFrame, season: int, week: int) -> dict[str, int]:
    """Compute DST-attributable points allowed for every team in a week.

    Returns ``{team_abbrev: points}``. Derived from play-by-play because
    nflverse exposes no points-allowed column, and because the correct figure
    is not the opponent's final score -- see the module docstring.
    """
    games = pbp.filter(
        (pl.col("season") == season)
        & (pl.col("week") == week)
        & pl.col("posteam").is_not_null()
    )
    if games.height == 0:
        raise ValueError(f"no play-by-play found for {season} week {week}")

    # Final scores, taken from the last play of each game.
    finals = (
        pbp.filter((pl.col("season") == season) & (pl.col("week") == week))
        .group_by("game_id")
        .agg(
            pl.col("home_team").drop_nulls().last().alias("home_team"),
            pl.col("away_team").drop_nulls().last().alias("away_team"),
            pl.col("total_home_score").max().alias("home_score"),
            pl.col("total_away_score").max().alias("away_score"),
        )
    )

    # Touchdowns scored by the team on defence, on a scrimmage play. These are
    # the only opponent points a DST is not charged for.
    excluded = (
        games.filter(
            (pl.col("touchdown") == 1)
            & (pl.col("td_team") == pl.col("defteam"))
            & pl.col("play_type").is_in(list(SCRIMMAGE_PLAY_TYPES))
        )
        .group_by("td_team")
        .len()
        .rename({"td_team": "team", "len": "defensive_tds"})
    )
    forgiven = {r["team"]: int(r["defensive_tds"]) for r in excluded.to_dicts()}

    def canonical(value: str | None) -> str | None:
        """Normalize through the alias map.

        nflverse is not internally consistent about team codes: play-by-play
        writes the Rams as ``LA`` while ``team_stats`` writes ``LAR``. Keying
        this dict on the raw play-by-play value means the caller -- which does
        resolve -- silently fails to find that team and drops it. Resolving on
        both sides is the only way the two agree.
        """
        if value is None:
            return None
        try:
            return resolve_team(value).abbrev
        except UnknownTeam:
            log.warning("unresolved team %r in play-by-play", value)
            return None

    forgiven = {
        canonical(team): count
        for team, count in forgiven.items()
        if canonical(team) is not None
    }

    allowed: dict[str, int] = {}
    for game in finals.to_dicts():
        home, away = canonical(game["home_team"]), canonical(game["away_team"])
        if home is None or away is None:
            continue
        for team, opponent, opponent_score in (
            (home, away, game["away_score"]),
            (away, home, game["home_score"]),
        ):
            # The opponent's defensive touchdowns were scored against OUR
            # offence, so they are not charged to our defence.
            charged = int(_num(opponent_score)) - _TOUCHDOWN_POINTS * forgiven.get(
                opponent, 0
            )
            allowed[team] = max(0, charged)
    return allowed


def offense_results(
    player_stats: pl.DataFrame, season: int, week: int
) -> list[PlayerResult]:
    """Score every offensive player in a week at DraftKings Classic rules."""
    rows = player_stats.filter(
        (pl.col("season") == season)
        & (pl.col("week") == week)
        & pl.col("position").is_in(list(OFFENSIVE_POSITIONS))
    )

    results = []
    for row in rows.to_dicts():
        team = row.get("team")
        try:
            resolved = resolve_team(team).abbrev if team else ""
        except UnknownTeam:
            log.warning("unresolved team %r for %s", team, row.get("player_id"))
            resolved = str(team)
        opponent = row.get("opponent_team")
        try:
            opponent = resolve_team(opponent).abbrev if opponent else ""
        except UnknownTeam:
            opponent = str(opponent)

        results.append(
            PlayerResult(
                season=season,
                week=week,
                entity_type="player",
                nflverse_id=row["player_id"],
                name=row.get("player_display_name") or row.get("player_name") or "",
                team=resolved,
                opponent=opponent,
                position=row.get("position") or "",
                dk_points=score_offense(offense_stats_from_row(row)),
            )
        )
    return results


def defense_results(
    team_stats: pl.DataFrame,
    points_allowed: dict[str, int],
    season: int,
    week: int,
) -> list[PlayerResult]:
    """Score every team defense in a week at DraftKings Classic rules.

    A defense is identified by its team abbreviation rather than a player id:
    it is a team-level entity, not a player with an odd name.
    """
    rows = team_stats.filter((pl.col("season") == season) & (pl.col("week") == week))

    results = []
    for row in rows.to_dicts():
        try:
            team = resolve_team(row["team"]).abbrev
        except UnknownTeam:
            log.warning("unresolved team %r in team_stats", row.get("team"))
            continue

        if team not in points_allowed:
            # Without an attributable points-allowed figure the tier is
            # unknowable, and defaulting to zero would award a shutout.
            log.warning("no points-allowed figure for %s; skipping", team)
            continue

        opponent = row.get("opponent_team") or ""
        try:
            opponent = resolve_team(opponent).abbrev if opponent else ""
        except UnknownTeam:
            opponent = str(opponent)

        stats = defense_stats_from_row(row, points_allowed[team])
        results.append(
            PlayerResult(
                season=season,
                week=week,
                entity_type="dst",
                nflverse_id=team,
                name=resolve_team(team).nickname,
                team=team,
                opponent=opponent,
                position="DST",
                dk_points=score_defense(stats),
            )
        )
    return results


def load_and_score_week(season: int, week: int) -> tuple[list[PlayerResult], bytes]:
    """Fetch nflverse tables for one week and score them.

    Returns the scored results and the raw JSON of the *source rows* used --
    not our computed output. Archiving the input is what allows a scoring bug
    found in December to be re-run against September's data; archiving only
    the output would bake the bug in permanently.

    Imported lazily because nflreadpy pulls large remote tables, and importing
    this module should stay cheap for callers that only want the mapping
    functions.
    """
    import json

    import nflreadpy as nfl

    log.info("loading nflverse tables for %s week %s", season, week)
    player_stats = nfl.load_player_stats(seasons=[season], summary_level="week")
    team_stats = nfl.load_team_stats(seasons=[season], summary_level="week")
    pbp = nfl.load_pbp(seasons=[season])

    allowed = points_allowed_by_team(pbp, season, week)
    results = offense_results(player_stats, season, week) + defense_results(
        team_stats, allowed, season, week
    )
    if not results:
        raise ValueError(f"nflverse returned no rows for {season} week {week}")

    source_rows = {
        "season": season,
        "week": week,
        "points_allowed": allowed,
        "player_stats": player_stats.filter(
            (pl.col("season") == season) & (pl.col("week") == week)
        ).to_dicts(),
        "team_stats": team_stats.filter(
            (pl.col("season") == season) & (pl.col("week") == week)
        ).to_dicts(),
    }
    raw = json.dumps(source_rows, default=str).encode("utf-8")
    return results, raw
