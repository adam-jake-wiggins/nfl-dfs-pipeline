"""Identity resolution: source-specific players onto stable nflverse ids.

This is the defect the prototype had. It exact-matched lowercased names and,
on a miss, silently substituted a season average -- producing plausible
lineups built on the wrong numbers, with no signal that anything had happened.

What the handoff assumed, and what is actually true
===================================================
The specification calls for mapping "DK player IDs ... onto stable nflverse
IDs" via the nflverse crosswalk. **nflverse carries no DraftKings id.** Its
``ff_playerids`` table cross-references MFL, Sportradar, FantasyPros, PFF,
Sleeper, ESPN, Yahoo, CBS, PFR and Rotowire -- verified 2026-08-17 -- and
DraftKings appears in none of them.

So DraftKings can only be joined by **name**, with team and position as
disambiguators. That makes the persistent crosswalk more important than the
handoff anticipated rather than less: a name match is expensive and fallible,
so it is done **once** and stored against DraftKings' *stable* ``playerDkId``.
Week 12 reuses Week 3's answer by id, and no name is matched twice.

Two reference tables, layered
=============================
Measured against a real 692-player slate:

===========================  =========  ==========
reference                    resolved   unmatched
===========================  =========  ==========
``ff_playerids`` alone           82.9%         118
``ff_playerids`` + ``players``   98.3%          12
===========================  =========  ==========

``ff_playerids`` is preferred because it carries cross-platform ids worth
having; ``players`` is broader (24k name keys against 8k) and catches rookies
and fringe roster players the fantasy-oriented table omits. The residue after
both is 12 players, median salary $3,000, **none above $5,000** -- punt plays
whose absence is visible in the match report rather than silent.

Why there is no fuzzy matching
==============================
The handoff permits fuzzy matching above a confidence threshold with team and
position agreement. It is deliberately not implemented, because exact
normalized matching plus team/position disambiguation already reaches 98.3%,
and the remaining 1.7% are the cheapest players on the slate.

Fuzzy matching there would be trading a *visible* miss for an *invisible*
wrong answer. A miss is reported, costs one punt play, and can be resolved by
hand once and stored forever. A bad fuzzy match silently attaches one player's
history to another and looks exactly like success. When the residue is
$3,000 punt plays, that trade is clearly bad.

If a later slate shows expensive players in the residue, this decision should
be revisited with that evidence -- which is the point of reporting the residue
rather than hiding it.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from dfs_pipeline.names import normalize_name
from dfs_pipeline.teams import UnknownTeam, is_free_agent, resolve_team

__all__ = [
    "MATCH_METHODS",
    "IdentityResolver",
    "MatchReport",
    "ReferencePlayer",
    "Resolution",
    "SUBJECT_SALARY_FLOOR",
]

log = logging.getLogger("dfs_pipeline.crosswalk")

#: Every way a resolution can be reached, recorded on each stored row so a
#: reviewer can tell a certainty from an inference.
MATCH_METHODS = (
    "id",           # a shared identifier existed (never DraftKings today)
    "normalized",   # exact normalized-name match, unambiguous
    "fuzzy",        # reserved; not implemented -- see the module docstring
    "manual",       # a human decided
    "dst_alias",    # team-level entity resolved through the 32-team map
    "unresolved",   # no answer; recorded so the miss is visible and reusable
)

#: Unmatched players at or above this salary are warned about individually.
#: A missing $3,000 punt play is noise; a missing $8,000 player is a hole.
SUBJECT_SALARY_FLOOR = 5000


@dataclass(frozen=True, slots=True)
class ReferencePlayer:
    """One nflverse player, indexed for matching."""

    nflverse_id: str
    name: str
    normalized: str
    team: str | None
    position: str | None
    source_table: str


@dataclass(frozen=True, slots=True)
class Resolution:
    """The outcome of resolving one source subject."""

    nflverse_id: str | None
    match_method: str
    confidence: float | None = None
    reference_name: str | None = None
    note: str | None = None

    @property
    def resolved(self) -> bool:
        return self.nflverse_id is not None


@dataclass
class MatchReport:
    """What a resolution pass did. Never silent, by construction."""

    total: int = 0
    by_method: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    unresolved: list[dict] = field(default_factory=list)
    ambiguous: list[dict] = field(default_factory=list)

    @property
    def resolved(self) -> int:
        return self.total - self.by_method.get("unresolved", 0)

    @property
    def match_rate(self) -> float:
        return self.resolved / self.total if self.total else 0.0

    def expensive_misses(self, floor: int = SUBJECT_SALARY_FLOOR) -> list[dict]:
        return sorted(
            (u for u in self.unresolved if (u.get("salary") or 0) >= floor),
            key=lambda u: -(u.get("salary") or 0),
        )

    def render(self, floor: int = SUBJECT_SALARY_FLOOR) -> str:
        lines = [
            f"identity: {self.resolved}/{self.total} resolved "
            f"({self.match_rate:.1%})"
        ]
        for method in MATCH_METHODS:
            count = self.by_method.get(method, 0)
            if count:
                lines.append(f"  {method:<12} {count:>5}")

        gaps = self.expensive_misses(floor)
        if gaps:
            lines.append(f"  WARNING: {len(gaps)} unresolved at ${floor:,}+:")
            for gap in gaps[:10]:
                lines.append(
                    f"    ${gap.get('salary', 0):>6,}  {gap.get('name')} "
                    f"({gap.get('position')} {gap.get('team')})"
                )
        if self.ambiguous:
            lines.append(
                f"  {len(self.ambiguous)} name(s) matched multiple nflverse "
                f"players and could not be separated by team or position"
            )
        return "\n".join(lines)


class IdentityResolver:
    """Resolves source players onto nflverse ids, reusing stored answers.

    A resolver holds an in-memory index of nflverse players plus, optionally,
    a :class:`~dfs_pipeline.store.SnapshotStore` whose ``crosswalk`` table
    persists what has already been decided.
    """

    def __init__(
        self,
        reference: Sequence[ReferencePlayer],
        *,
        store=None,
    ) -> None:
        self.store = store
        self._by_name: dict[str, list[ReferencePlayer]] = defaultdict(list)
        for player in reference:
            if player.normalized:
                self._by_name[player.normalized].append(player)

    # -- construction ------------------------------------------------------

    @classmethod
    def from_nflverse(cls, *, store=None, seasons: Iterable[int] | None = None):
        """Build from nflverse, layering both reference tables.

        Imported lazily: nflreadpy pulls large remote tables, and constructing
        a resolver from an explicit reference list (as tests do) should not
        require the network.
        """
        import nflreadpy as nfl
        import polars as pl

        reference: list[ReferencePlayer] = []
        # Keyed on (id, normalized name), NOT on id alone. The same player is
        # spelled differently across tables -- ff_playerids says "Kenneth
        # Gainwell" while players and DraftKings say "Kenny Gainwell", both
        # gsis 00-0036919. Deduplicating by id would keep the first spelling
        # and discard the second, throwing away precisely the aliases this
        # module exists to resolve. Every distinct spelling is indexed, all
        # pointing at the same id.
        seen: set[tuple[str, str]] = set()

        # ff_playerids first: it carries cross-platform ids worth having.
        ff = nfl.load_ff_playerids().filter(pl.col("gsis_id").is_not_null())
        for row in ff.select(["name", "position", "team", "gsis_id"]).to_dicts():
            entry = _reference_from(row, "name", "ff_playerids")
            if entry and (key := (entry.nflverse_id, entry.normalized)) not in seen:
                reference.append(entry)
                seen.add(key)

        # players second: broader roster coverage, catching rookies and fringe
        # players the fantasy-oriented table omits.
        players = nfl.load_players().filter(pl.col("gsis_id").is_not_null())
        name_column = (
            "display_name" if "display_name" in players.columns else "full_name"
        )
        columns = [name_column, "position", "gsis_id"]
        if "latest_team" in players.columns:
            columns.append("latest_team")
        for row in players.select(columns).to_dicts():
            row.setdefault("team", row.get("latest_team"))
            entry = _reference_from(row, name_column, "players")
            if entry and (key := (entry.nflverse_id, entry.normalized)) not in seen:
                reference.append(entry)
                seen.add(key)

        log.info("identity reference: %d nflverse players", len(reference))
        return cls(reference, store=store)

    # -- resolution --------------------------------------------------------

    def resolve(
        self,
        *,
        source: str,
        source_subject_id: str,
        name: str,
        entity_type: str = "player",
        team: str | None = None,
        position: str | None = None,
    ) -> Resolution:
        """Resolve one subject, preferring a stored answer over a fresh match.

        Reusing a stored resolution is not merely an optimisation. A name match
        is fallible, so deciding once and keeping the answer means a player
        cannot be resolved one way in Week 3 and another way in Week 12 because
        an upstream table shifted underneath us.
        """
        stored = self._stored(source, source_subject_id)
        if stored is not None:
            return stored

        if entity_type == "dst":
            resolution = self._resolve_defense(name, team)
        else:
            resolution = self._resolve_player(name, team, position)

        self._persist(
            source=source,
            source_subject_id=source_subject_id,
            name=name,
            team=team,
            position=position,
            entity_type=entity_type,
            resolution=resolution,
        )
        return resolution

    def _resolve_defense(self, name: str, team: str | None) -> Resolution:
        """A defense is a team-level entity, never a player with an odd name.

        Resolved through the exhaustive 32-team map, so no fuzzy or
        name-similarity logic can ever run against a defense.
        """
        for candidate in (team, name):
            if not candidate:
                continue
            try:
                resolved = resolve_team(candidate)
            except UnknownTeam:
                continue
            return Resolution(
                nflverse_id=resolved.abbrev,
                match_method="dst_alias",
                confidence=1.0,
                reference_name=resolved.nickname,
            )
        return Resolution(
            nflverse_id=None,
            match_method="unresolved",
            note=f"no team matched {name!r}",
        )

    def _resolve_player(
        self, name: str, team: str | None, position: str | None
    ) -> Resolution:
        key = normalize_name(name)
        candidates = self._by_name.get(key, [])

        if not candidates:
            return Resolution(
                nflverse_id=None,
                match_method="unresolved",
                note="no nflverse player with this normalized name",
            )

        # Several spellings of ONE player is not ambiguity -- collapse first.
        distinct = {c.nflverse_id for c in candidates}
        if len(distinct) == 1:
            best = candidates[0]
            return Resolution(
                nflverse_id=best.nflverse_id,
                match_method="normalized",
                confidence=1.0,
                reference_name=best.name,
            )

        # Ambiguous. Narrow by team AND position first, then position alone --
        # a player's team changes mid-season far more often than their
        # position does, so position is the more reliable discriminator.
        narrowed = [
            c for c in candidates
            if _same_team(c.team, team) and _same_position(c.position, position)
        ]
        if len(narrowed) != 1:
            narrowed = [c for c in candidates if _same_position(c.position, position)]

        if len(narrowed) == 1:
            return Resolution(
                nflverse_id=narrowed[0].nflverse_id,
                match_method="normalized",
                confidence=0.9,
                reference_name=narrowed[0].name,
                note="disambiguated by team and position",
            )

        return Resolution(
            nflverse_id=None,
            match_method="unresolved",
            note=(
                f"{len(candidates)} nflverse players share this name and could "
                f"not be separated by team or position"
            ),
        )

    # -- batch -------------------------------------------------------------

    def resolve_slate(self, players, *, source: str) -> tuple[dict, MatchReport]:
        """Resolve a whole slate, returning ids and a report.

        The report is the point. A match rate nobody sees is a match rate
        nobody checks, which is exactly how the prototype degraded silently.
        """
        report = MatchReport()
        resolved: dict[str, Resolution] = {}

        for player in players:
            report.total += 1
            outcome = self.resolve(
                source=source,
                source_subject_id=player.source_player_id,
                name=player.name,
                entity_type=player.entity_type,
                team=player.team,
                position=player.position,
            )
            resolved[player.source_player_id] = outcome
            report.by_method[outcome.match_method] += 1

            if not outcome.resolved:
                record = {
                    "source_subject_id": player.source_player_id,
                    "name": player.name,
                    "team": player.team,
                    "position": player.position,
                    "salary": getattr(player, "salary", None),
                    "note": outcome.note,
                }
                report.unresolved.append(record)
                if outcome.note and "share this name" in outcome.note:
                    report.ambiguous.append(record)

        return resolved, report

    # -- persistence -------------------------------------------------------

    def _stored(self, source: str, source_subject_id: str) -> Resolution | None:
        if self.store is None:
            return None
        row = self.store._con.execute(
            "SELECT nflverse_id, match_method, confidence, review_status "
            "FROM crosswalk WHERE source = ? AND source_subject_id = ?",
            (source, str(source_subject_id)),
        ).fetchone()
        if row is None:
            return None
        if row["review_status"] == "rejected":
            # A human looked at this and said no. Re-deriving it would undo
            # their decision every week.
            return Resolution(
                nflverse_id=None,
                match_method="unresolved",
                note="a previous resolution was rejected on review",
            )
        return Resolution(
            nflverse_id=row["nflverse_id"],
            match_method=row["match_method"],
            confidence=row["confidence"],
            note="reused from the stored crosswalk",
        )

    def _persist(
        self, *, source, source_subject_id, name, team, position,
        entity_type, resolution: Resolution,
    ) -> None:
        if self.store is None:
            return
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.store._con.execute(
            "INSERT INTO crosswalk "
            "(source, source_subject_id, source_name, team, position, "
            " entity_type, nflverse_id, match_method, confidence, "
            " review_status, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source, source_subject_id) DO UPDATE SET "
            "  last_seen = excluded.last_seen, "
            "  source_name = excluded.source_name",
            (
                source, str(source_subject_id), name, team, position,
                entity_type, resolution.nflverse_id, resolution.match_method,
                resolution.confidence, "pending", now, now,
            ),
        )


def _reference_from(row: dict, name_column: str, table: str) -> ReferencePlayer | None:
    identifier = row.get("gsis_id")
    name = row.get(name_column)
    if not identifier or not name:
        return None
    team = row.get("team") or row.get("latest_team")
    if team and is_free_agent(team):
        team = None
    elif team:
        try:
            team = resolve_team(team).abbrev
        except UnknownTeam:
            team = None
    return ReferencePlayer(
        nflverse_id=str(identifier),
        name=str(name),
        normalized=normalize_name(str(name)),
        team=team,
        position=(row.get("position") or None),
        source_table=table,
    )


def _same_team(reference: str | None, source: str | None) -> bool:
    if not reference or not source:
        return False
    try:
        return resolve_team(reference).abbrev == resolve_team(source).abbrev
    except UnknownTeam:
        return False


def _same_position(reference: str | None, source: str | None) -> bool:
    if not reference or not source:
        return False
    return reference.strip().upper() == source.strip().upper()
