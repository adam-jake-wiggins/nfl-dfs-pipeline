"""DraftKings draftables endpoint, isolated behind one adapter.

Safety boundary
===============
This module is **read-only and unauthenticated, by construction**. It does not
and must not implement authentication, session or cookie handling, lineup
upload, entry submission, contest registration, or any account mutation. No
code path here acts as the operator.

That is not a convention -- ``test_no_authentication_code_exists`` reads this
file and fails if the words appear. The rule is easiest to keep while it costs
nothing, and a future edit that adds a login is exactly the change that should
be hard to make by accident.

Practically: no ``requests.Session`` (which would persist cookies), no
credentials, no POST, no PUT, no DELETE. A descriptive User-Agent, a timeout,
and no retry loop -- a failed call returns an error and the operator falls back
to the CSV path, which is a first-class equal rather than a degraded mode.

Stability
=========
These endpoints are undocumented and carry no stability guarantee. They can
change shape or vanish without notice, plausibly mid-season. That is precisely
why they live behind one class producing the same
:class:`~dfs_pipeline.adapters.base.SlatePlayer` records as the CSV importer:
when they break, one module changes and the pipeline does not.

The FLEX duplication trap
=========================
The draftables response has substantially more rows than players. For the 2026
Week 1 main slate: **1,317 rows for 716 players**. The excess is exactly
153 RB + 293 WR + 155 TE = 601, because DraftKings issues a *separate*
``draftableId`` for each roster slot a player is eligible for::

    Jahmyr Gibbs  draftableId=43727325  rosterSlotId=67 (RB)    salary=8000
    Jahmyr Gibbs  draftableId=43727326  rosterSlotId=70 (FLEX)  salary=8000

An adapter treating each row as a player would create 601 phantom entries and
could build a lineup containing the same person twice under two different ids
-- contest-illegal, and invisible to every constraint check, because the solver
sees two distinct ids. Rows are therefore grouped by ``playerDkId``.

What this path knows that the CSV cannot
========================================
- ``playerDkId``: a **stable** player identifier. ``draftableId`` changes every
  slate; this does not, which is what a persistent crosswalk should key on.
- ``competition.startTime``: lock time as unambiguous UTC, rather than the
  CSV's ``09/13/2026 01:00PM ET`` which must be parsed and resolved across the
  daylight-saving boundary.
- ``draftGroupId`` and stable ``competitionId`` values.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from dfs_pipeline.adapters.base import GameInfo, SlatePlayer, SlateSchemaError
from dfs_pipeline.teams import UnknownTeam, resolve_team

__all__ = [
    "DraftKingsApiAdapter",
    "DraftGroup",
    "DraftKingsApiError",
    "ROSTER_SLOTS",
    "LOBBY_URL",
    "DRAFTABLES_URL",
]

log = logging.getLogger("dfs_pipeline.dk_api")

LOBBY_URL = "https://www.draftkings.com/lobby/getcontests"
DRAFTABLES_URL = "https://api.draftkings.com/draftgroups/v1/draftgroups/{id}/draftables"

#: Identifies this tool honestly rather than impersonating a browser.
USER_AGENT = "nfl-dfs-pipeline/0.1 (read-only slate capture)"

#: Roster slot ids, inferred from a real response and asserted in tests.
ROSTER_SLOTS = {66: "QB", 67: "RB", 68: "WR", 69: "TE", 70: "FLEX", 71: "DST"}

#: The FLEX slot duplicates a player already present under their position.
FLEX_SLOT_ID = 70

#: DraftKings labels the contest format in the lobby payload. Verified
#: 2026-08-17 against a live listing:
#:
#:   1   Classic salary cap   <- the only format this pipeline supports
#:   96  Showdown (single game)
#:   145 Sit & Go (SNAKE DRAFT -- draftables carry no salaries at all)
#:   158 / 159  Madden Stream (simulated video-game contests)
#:
#: An earlier version of this adapter inferred the format from game count and
#: contest name, and duly auto-selected a 16-game Sit & Go whose 4,501
#: draftables had no salary field. DraftKings states the format outright;
#: guessing at it was the bug.
CLASSIC_GAME_TYPE_ID = 1

#: DraftKings runs simulated "Madden Stream" contests alongside real NFL. They
#: are real draft groups with real salaries and are not football. Kept as
#: defence in depth even though the game-type check already excludes them.
SIMULATED_MARKERS = ("madden", "simulated")


class DraftKingsApiError(RuntimeError):
    """A DraftKings request failed or returned something unusable."""


@dataclass(frozen=True, slots=True)
class DraftGroup:
    """One slate offered by DraftKings."""

    draft_group_id: int
    game_count: int
    start_time: str
    suffix: str
    tag: str
    game_type_id: int | None = None

    @property
    def is_simulated(self) -> bool:
        text = f"{self.suffix} {self.tag}".lower()
        return any(marker in text for marker in SIMULATED_MARKERS)

    @property
    def is_classic(self) -> bool:
        """Whether DraftKings labels this a salary-cap Classic contest."""
        return self.game_type_id == CLASSIC_GAME_TYPE_ID

    @property
    def is_classic_candidate(self) -> bool:
        """A multi-game salary-cap slate this pipeline can actually use."""
        return self.is_classic and self.game_count >= 2 and not self.is_simulated


class DraftKingsApiAdapter:
    """Fetches a DraftKings slate and normalizes it to :class:`SlatePlayer`.

    Satisfies the same ``SalarySource`` protocol as the CSV importer, so
    downstream code cannot tell which produced a slate.
    """

    source_name = "DK_API"

    def __init__(
        self,
        draft_group_id: int | None = None,
        *,
        timeout: float = 30.0,
        min_games: int = 2,
    ) -> None:
        self.draft_group_id = draft_group_id
        self.timeout = timeout
        self.min_games = min_games

    # -- http --------------------------------------------------------------

    def _get(self, url: str, params: dict | None = None) -> requests.Response:
        """One GET. No session, no cookies, no credentials, no retry loop.

        A session would persist cookies across calls, which is the first step
        toward carrying an identity. Plain per-call GETs keep that impossible.
        """
        try:
            response = requests.get(
                url,
                params=params,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=self.timeout,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            raise DraftKingsApiError(
                f"could not reach DraftKings: {exc}. The manual DKSalaries.csv "
                f"import is a fully supported alternative."
            ) from None

        if response.status_code != 200:
            raise DraftKingsApiError(
                f"DraftKings returned HTTP {response.status_code} for {url}. "
                f"These endpoints are undocumented and may have changed; the "
                f"manual DKSalaries.csv import remains available."
            )
        return response

    # -- discovery ---------------------------------------------------------

    def discover_draft_groups(self) -> list[DraftGroup]:
        """List NFL draft groups currently offered.

        Note this response is large (~2.5 MB) because the lobby payload
        carries every contest as well. Pass ``draft_group_id`` explicitly to
        skip it entirely.
        """
        payload = self._get(LOBBY_URL, {"sport": "NFL"}).json()
        raw = payload.get("DraftGroups")
        if not isinstance(raw, list):
            raise SlateSchemaError(
                "lobby response", "no DraftGroups list in the lobby payload"
            )

        groups = [
            DraftGroup(
                draft_group_id=int(g["DraftGroupId"]),
                game_count=int(g.get("GameCount") or 0),
                start_time=str(g.get("StartDate") or ""),
                suffix=str(g.get("ContestStartTimeSuffix") or ""),
                tag=str(g.get("DraftGroupTag") or ""),
                game_type_id=(
                    int(g["GameTypeId"]) if g.get("GameTypeId") is not None else None
                ),
            )
            for g in raw
            if g.get("DraftGroupId") is not None
        ]
        log.info("discovered %d NFL draft groups", len(groups))
        return groups

    def find_main_slate(self) -> DraftGroup:
        """Pick the largest non-simulated multi-game slate.

        "Main slate" has no official marker, so this is a heuristic and is
        documented as one: most games wins, ties broken by earliest start.
        The operator can always pass ``draft_group_id`` explicitly, which is
        the right move when it matters.
        """
        candidates = [g for g in self.discover_draft_groups() if g.is_classic_candidate]
        if not candidates:
            raise DraftKingsApiError(
                f"no multi-game salary-cap Classic draft group found "
                f"(GameTypeId {CLASSIC_GAME_TYPE_ID}). Out of season, or "
                f"DraftKings is currently offering only Showdown, Sit & Go or "
                f"simulated contests. Pass --draft-group to select one "
                f"explicitly."
            )
        return sorted(candidates, key=lambda g: (-g.game_count, g.start_time))[0]

    # -- fetch -------------------------------------------------------------

    def raw_bytes(self) -> bytes:
        group_id = self.draft_group_id
        if group_id is None:
            chosen = self.find_main_slate()
            group_id = chosen.draft_group_id
            self.draft_group_id = group_id
            log.info(
                "selected draft group %s (%d games, starts %s)",
                group_id, chosen.game_count, chosen.start_time,
            )
        return self._get(DRAFTABLES_URL.format(id=group_id)).content

    # -- parsing -----------------------------------------------------------

    def loads(self, raw: bytes) -> list[SlatePlayer]:
        """Normalize a draftables response, taking bytes so a stored artifact
        re-parses through exactly the code that parsed it originally."""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SlateSchemaError(
                "draftables response", f"not valid JSON: {exc}"
            ) from None

        entries = payload.get("draftables")
        if not isinstance(entries, list):
            raise SlateSchemaError(
                "draftables response",
                "no 'draftables' list in the payload. The endpoint may have "
                "changed shape; use the manual DKSalaries.csv import.",
            )
        if not entries:
            raise SlateSchemaError("draftables response", "draft group has no players")

        # Group by the STABLE player id. Each roster slot a player is eligible
        # for arrives as its own row with its own draftableId.
        by_player: dict[object, list[dict]] = {}
        for entry in entries:
            key = entry.get("playerDkId")
            if key is None:
                raise SlateSchemaError(
                    "draftables response",
                    f"entry {entry.get('draftableId')} has no playerDkId, so "
                    f"roster-slot duplicates cannot be collapsed safely",
                )
            by_player.setdefault(key, []).append(entry)

        players = [
            self._build(player_id, rows) for player_id, rows in by_player.items()
        ]
        self._require_enough_games(players)
        return players

    def _build(self, player_dk_id: object, rows: list[dict]) -> SlatePlayer:
        # The CSV carries the position-slot id, never the FLEX one, so the
        # primary row must match for the two paths to agree on source ids.
        primary = next(
            (r for r in rows if r.get("rosterSlotId") != FLEX_SLOT_ID), rows[0]
        )

        name = (primary.get("displayName") or "").strip()
        position = (primary.get("position") or "").strip().upper()
        team_raw = (primary.get("teamAbbreviation") or "").strip()
        salary = primary.get("salary")

        if salary is None:
            # Verified 2026-08-17: a Sit & Go draft group returned 4,501
            # draftables with no salary field at all, because snake drafts do
            # not price players. Naming the likely cause turns a confusing
            # parse error into an actionable one.
            raise SlateSchemaError(
                "draftables response",
                f"entry {primary.get('draftableId')} ({name or 'unnamed'}) has no "
                f"salary. Draft group {self.draft_group_id} is probably not a "
                f"salary-cap Classic slate -- Sit & Go and other draft-style "
                f"contests carry no salaries. Check the draft group id.",
            )
        if not name or not position:
            raise SlateSchemaError(
                "draftables response",
                f"entry {primary.get('draftableId')} missing name or position",
            )

        try:
            team = resolve_team(team_raw).abbrev if team_raw else ""
        except UnknownTeam as exc:
            raise SlateSchemaError("draftables response", str(exc)) from None

        competition = primary.get("competition") or {}
        game = self._game_from(competition, name)

        slots = tuple(
            sorted(
                {
                    ROSTER_SLOTS[r["rosterSlotId"]]
                    for r in rows
                    if r.get("rosterSlotId") in ROSTER_SLOTS
                }
            )
        )

        return SlatePlayer(
            source_player_id=str(primary["draftableId"]),
            name=name,
            position=position,
            salary=int(salary),
            team=team,
            game=game,
            entity_type="dst" if position == "DST" else "player",
            roster_positions=slots,
            avg_points_per_game=_avg_points(primary),
            status=_status(primary),
            draft_group_id=self.draft_group_id,
            lock_time_utc=game.kickoff_utc,
            stable_player_id=str(player_dk_id),
        )

    def _game_from(self, competition: dict, who: str) -> GameInfo:
        name = (competition.get("name") or "").strip()
        if "@" not in name:
            raise SlateSchemaError(
                "draftables response",
                f"could not read a matchup from competition name {name!r} for {who!r}",
            )
        away_raw, _, home_raw = name.partition("@")
        try:
            away = resolve_team(away_raw.strip()).abbrev
            home = resolve_team(home_raw.strip()).abbrev
        except UnknownTeam as exc:
            raise SlateSchemaError("draftables response", str(exc)) from None

        return GameInfo(
            away_team=away, home_team=home,
            kickoff_utc=_utc(competition.get("startTime")),
        )

    def _require_enough_games(self, players: list[SlatePlayer]) -> None:
        games = {p.game.key for p in players}
        if len(games) < self.min_games:
            raise SlateSchemaError(
                "draftables response",
                f"only {len(games)} game(s) present ({', '.join(sorted(games))}). "
                f"This looks like a Showdown slate; Classic requires at least "
                f"{self.min_games}.",
            )


def _utc(value: object) -> str | None:
    """DraftKings writes 7-digit fractional seconds, which fromisoformat rejects."""
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(c for c in tail if c.isdigit())[:6]
        offset = tail[len(digits):] if len(tail) > len(digits) else ""
        # Preserve any timezone suffix that followed the fraction.
        for marker in ("+", "-"):
            if marker in tail:
                offset = tail[tail.index(marker):]
                break
        text = f"{head}.{digits or '0'}{offset}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _avg_points(entry: dict) -> float | None:
    """Pull AvgPointsPerGame out of draftStatAttributes (attribute id 90)."""
    for attribute in entry.get("draftStatAttributes") or []:
        if attribute.get("id") == 90:
            try:
                return float(attribute.get("value"))
            except (TypeError, ValueError):
                return None
    return None


def _status(entry: dict) -> str | None:
    """DraftKings writes 'None' as a string for a clear player."""
    value = (entry.get("status") or "").strip()
    return None if value in ("", "None") else value.upper()
