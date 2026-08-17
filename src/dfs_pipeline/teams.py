"""Canonical NFL team identity, and the alias map that resolves onto it.

Three upstream sources spell teams three ways. DraftKings uses abbreviations
(``KC``) and names defenses by nickname (``Chiefs``). The Odds API uses full
names (``Kansas City Chiefs``). nflverse has its own conventions, and
historical data carries relocated-franchise codes (``SD``, ``OAK``, ``STL``).

Teams are the *easy* identity problem -- there are exactly 32, they change
about once a decade, and the mapping can be exhaustive rather than fuzzy.
That is why this module contains no fuzzy matching at all: an unknown team is
an error, not a guess. Player identity is the hard problem and is handled
separately, against the crosswalk.

This module also satisfies the DST alias requirement. A defense is not a
player with an odd name; it is a team-level entity, and resolving ``Chiefs``
to ``KC`` must never run through logic designed for human names.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "Team",
    "TEAMS",
    "resolve_team",
    "UnknownTeam",
    "canonical_abbreviations",
]


class UnknownTeam(KeyError):
    """Raised when a team string cannot be resolved.

    Deliberately an error rather than a ``None``. A silently unresolved team
    would drop that team's odds from a slate, leaving the pipeline apparently
    healthy while half a game is missing.
    """

    def __init__(self, value: str) -> None:
        self.value = value
        super().__init__(
            f"unknown NFL team: {value!r}. Add it to dfs_pipeline.teams if this "
            f"is a legitimate new spelling."
        )


@dataclass(frozen=True, slots=True)
class Team:
    """One franchise, with every spelling we expect to encounter."""

    abbrev: str          #: canonical code, matching DraftKings' TeamAbbrev
    location: str        #: "Kansas City"
    nickname: str        #: "Chiefs" -- how DraftKings names the defense
    aliases: tuple[str, ...] = ()   #: historical or alternate codes

    @property
    def full_name(self) -> str:
        return f"{self.location} {self.nickname}"


#: All 32 franchises. Abbreviations verified 2026-08-17 against a real
#: DraftKings salary export; full names verified against a real Odds API
#: response the same day.
TEAMS: tuple[Team, ...] = (
    Team("ARI", "Arizona", "Cardinals", ("ARZ",)),
    Team("ATL", "Atlanta", "Falcons"),
    Team("BAL", "Baltimore", "Ravens", ("BLT",)),
    Team("BUF", "Buffalo", "Bills"),
    Team("CAR", "Carolina", "Panthers"),
    Team("CHI", "Chicago", "Bears"),
    Team("CIN", "Cincinnati", "Bengals"),
    Team("CLE", "Cleveland", "Browns", ("CLV",)),
    Team("DAL", "Dallas", "Cowboys"),
    Team("DEN", "Denver", "Broncos"),
    Team("DET", "Detroit", "Lions"),
    Team("GB", "Green Bay", "Packers", ("GNB",)),
    Team("HOU", "Houston", "Texans", ("HST",)),
    Team("IND", "Indianapolis", "Colts"),
    Team("JAX", "Jacksonville", "Jaguars", ("JAC",)),
    Team("KC", "Kansas City", "Chiefs", ("KAN",)),
    # Relocated franchises keep their historical codes as aliases so that
    # older nflverse seasons resolve without special-casing at the call site.
    Team("LAC", "Los Angeles", "Chargers", ("SD", "SDG")),
    Team("LAR", "Los Angeles", "Rams", ("LA", "STL", "RAM")),
    Team("LV", "Las Vegas", "Raiders", ("OAK", "LVR", "RAI")),
    Team("MIA", "Miami", "Dolphins"),
    Team("MIN", "Minnesota", "Vikings"),
    Team("NE", "New England", "Patriots", ("NWE",)),
    Team("NO", "New Orleans", "Saints", ("NOR",)),
    Team("NYG", "New York", "Giants"),
    Team("NYJ", "New York", "Jets"),
    Team("PHI", "Philadelphia", "Eagles"),
    Team("PIT", "Pittsburgh", "Steelers"),
    Team("SEA", "Seattle", "Seahawks"),
    Team("SF", "San Francisco", "49ers", ("SFO",)),
    Team("TB", "Tampa Bay", "Buccaneers", ("TAM",)),
    Team("TEN", "Tennessee", "Titans", ("OTI",)),
    # Renamed 2022; both former identities appear in historical data.
    Team("WAS", "Washington", "Commanders", ("WSH", "WFT")),
)


def canonical_abbreviations() -> frozenset[str]:
    return frozenset(t.abbrev for t in TEAMS)


def _normalize(value: str) -> str:
    return " ".join(value.strip().lower().replace(".", "").split())


def _build_lookup() -> dict[str, Team]:
    """Index every spelling once, refusing to let two teams claim the same key.

    Collisions are raised at import time rather than silently resolved by
    insertion order: an alias quietly shadowing another team's would misroute
    an entire franchise's data with no visible symptom.
    """
    lookup: dict[str, Team] = {}

    def add(key: str, team: Team) -> None:
        norm = _normalize(key)
        existing = lookup.get(norm)
        if existing is not None and existing is not team:
            raise AssertionError(
                f"alias collision: {key!r} maps to both "
                f"{existing.abbrev} and {team.abbrev}"
            )
        lookup[norm] = team

    for team in TEAMS:
        add(team.abbrev, team)
        add(team.full_name, team)
        add(team.nickname, team)
        for alias in team.aliases:
            add(alias, team)
    return lookup


#: Nicknames shared by more than one franchise would need location context.
#: There are none in the current NFL -- asserted at import, so a future
#: expansion team cannot introduce ambiguity unnoticed.
_LOOKUP: dict[str, Team] = _build_lookup()


def resolve_team(value: str) -> Team:
    """Resolve any known spelling of a team to its canonical :class:`Team`.

    Accepts abbreviations (``KC``), full names (``Kansas City Chiefs``),
    bare nicknames (``Chiefs``) and historical codes (``OAK``). Matching is
    exact after case and punctuation normalisation -- never fuzzy. With only
    32 possible answers, a fuzzy match that fires is a bug, not a rescue.
    """
    if not value or not value.strip():
        raise UnknownTeam(value)
    try:
        return _LOOKUP[_normalize(value)]
    except KeyError:
        raise UnknownTeam(value) from None
