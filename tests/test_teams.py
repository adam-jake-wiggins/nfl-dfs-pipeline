"""Tests for canonical NFL team identity.

Teams are the tractable half of the identity problem: 32 of them, changing
about once a decade, so the mapping can be exhaustive rather than fuzzy. These
tests hold it to that standard -- every spelling from every real source must
resolve exactly, and anything unrecognised must raise rather than guess.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from dfs_pipeline.teams import (
    TEAMS,
    Team,
    UnknownTeam,
    canonical_abbreviations,
    resolve_team,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_there_are_thirty_two_teams():
    assert len(TEAMS) == 32
    assert len(canonical_abbreviations()) == 32


def test_abbreviations_are_unique():
    assert len({t.abbrev for t in TEAMS}) == 32


def test_nicknames_are_unique():
    """Two franchises sharing a nickname would make DST resolution ambiguous."""
    assert len({t.nickname for t in TEAMS}) == 32


@pytest.mark.parametrize(
    "spelling, expected",
    [
        ("KC", "KC"),
        ("kc", "KC"),
        ("  KC  ", "KC"),
        ("Kansas City Chiefs", "KC"),
        ("kansas city chiefs", "KC"),
        ("Chiefs", "KC"),
        ("chiefs", "KC"),
    ],
)
def test_every_spelling_of_one_team_resolves(spelling, expected):
    assert resolve_team(spelling).abbrev == expected


@pytest.mark.parametrize(
    "historical, current",
    [
        ("OAK", "LV"),      # Oakland -> Las Vegas
        ("SD", "LAC"),      # San Diego -> Los Angeles
        ("STL", "LAR"),     # St Louis -> Los Angeles
        ("LA", "LAR"),      # ambiguous in the wild; we bind it to the Rams
        ("WSH", "WAS"),     # alternate abbreviation
        ("WFT", "WAS"),     # Washington Football Team, 2020-2021
        ("JAC", "JAX"),     # alternate abbreviation
    ],
)
def test_historical_and_alternate_codes_resolve(historical, current):
    """Older nflverse seasons carry relocated-franchise codes.

    Handling them here means no call site needs a special case.
    """
    assert resolve_team(historical).abbrev == current


@pytest.mark.parametrize("value", ["", "   ", "XYZ", "Not A Team", "Chief", "Chiefs FC"])
def test_unknown_teams_raise_rather_than_guess(value):
    """A silently unresolved team drops half a game's odds from the slate.

    With only 32 possible answers, a fuzzy match that fires is a bug rather
    than a rescue -- so there is no fuzzy matching to fire.
    """
    with pytest.raises(UnknownTeam):
        resolve_team(value)


def test_unknown_team_error_names_the_offender_and_the_fix():
    with pytest.raises(UnknownTeam) as exc:
        resolve_team("Toronto Huskies")
    message = str(exc.value)
    assert "Toronto Huskies" in message
    assert "dfs_pipeline.teams" in message


def test_punctuation_is_normalised_away():
    """Sources write "K.C." and "A.J." inconsistently; periods are noise."""
    assert resolve_team("K.C.").abbrev == "KC"
    assert resolve_team("S.F.").abbrev == "SF"


def test_full_name_is_derived_not_duplicated():
    kc = resolve_team("KC")
    assert kc.full_name == "Kansas City Chiefs"
    assert kc.location == "Kansas City"
    assert kc.nickname == "Chiefs"


def test_alias_collisions_are_caught_at_import():
    """Guards the guard: a shadowing alias must fail loudly, not silently win.

    An alias quietly claiming another team's key would misroute an entire
    franchise's data with no visible symptom.
    """
    from dfs_pipeline import teams as teams_module

    original = teams_module.TEAMS
    try:
        teams_module.TEAMS = (
            Team("AAA", "Alpha", "Antelopes", ("SHARED",)),
            Team("BBB", "Beta", "Bobcats", ("SHARED",)),
        )
        with pytest.raises(AssertionError, match="alias collision"):
            teams_module._build_lookup()
    finally:
        teams_module.TEAMS = original


# ---------------------------------------------------------------------------
# Against the real vocabularies of both live sources
# ---------------------------------------------------------------------------

def test_every_draftkings_abbreviation_resolves_to_itself():
    raw = (FIXTURES / "dk_salaries_real_shape.csv").read_bytes().decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(raw)))
    abbrevs = {r["TeamAbbrev"] for r in rows}
    assert abbrevs, "fixture has no teams"
    for abbrev in abbrevs:
        assert resolve_team(abbrev).abbrev == abbrev


def test_every_draftkings_defense_nickname_resolves():
    """DK names defenses by nickname; TeamAbbrev carries the code.

    Resolving 'Chargers' to 'LAC' must never run through logic designed for
    human names -- a defense is a team-level entity, not an odd player.
    """
    raw = (FIXTURES / "dk_salaries_real_shape.csv").read_bytes().decode("utf-8-sig")
    rows = [r for r in csv.DictReader(io.StringIO(raw)) if r["Position"] == "DST"]
    assert rows, "fixture has no defenses"
    for row in rows:
        assert resolve_team(row["Name"]).abbrev == row["TeamAbbrev"]


def test_every_odds_api_full_name_resolves():
    events = json.loads((FIXTURES / "odds_nfl_sample.json").read_text())
    names = {t for e in events for t in (e["home_team"], e["away_team"])}
    assert names, "fixture has no teams"
    for name in names:
        resolve_team(name)


def test_the_two_sources_agree_on_identity():
    """The whole point: DK's 'Chargers' and the Odds API's 'Los Angeles
    Chargers' must land on the same object, or odds cannot be joined to a
    slate."""
    for dk_spelling, odds_spelling in [
        ("Jaguars", "Jacksonville Jaguars"),
        ("Commanders", "Washington Commanders"),
        ("Chargers", "Los Angeles Chargers"),
        ("Raiders", "Las Vegas Raiders"),
        ("49ers", "San Francisco 49ers"),
    ]:
        assert resolve_team(dk_spelling) is resolve_team(odds_spelling)
