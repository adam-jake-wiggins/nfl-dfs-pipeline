"""Normalized slate representation shared by every salary source.

The DraftKings CSV export and the unofficial draftables endpoint carry the
same information in different shapes. Both converge here, immediately, so
that nothing downstream needs to know which one produced the data. When the
endpoint changes -- and it will, since it carries no stability guarantee --
one adapter changes and the pipeline does not.

Fields the CSV cannot supply (draft group id, lock time) are optional rather
than invented. A ``None`` that means "this path cannot know" is honest; a
default that looks like data is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

__all__ = [
    "SlatePlayer",
    "GameInfo",
    "SalarySource",
    "SlateSchemaError",
    "parse_game_info",
    "DK_TIMEZONE",
]

#: DraftKings publishes slate times in US Eastern, labelled "ET". Storing the
#: local wall-clock string would be ambiguous across the DST boundary, which
#: falls mid-season, so times are resolved to UTC on ingest.
DK_TIMEZONE = ZoneInfo("America/New_York")

#: e.g. "KC@BUF 09/13/2026 01:00PM ET" -- also tolerates a bare "KC@BUF".
_GAME_INFO_RE = re.compile(
    r"^\s*(?P<away>[A-Z]{2,4})\s*@\s*(?P<home>[A-Z]{2,4})"
    r"(?:\s+(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<time>\d{1,2}:\d{2}[AP]M)\s*(?P<tz>[A-Z]{2,3}))?\s*$"
)


class SlateSchemaError(ValueError):
    """A slate file did not have the shape we require.

    Carries the file, and where available the row and column, because
    "invalid CSV" is not an actionable message when the file has 700 rows.
    """

    def __init__(
        self,
        path: str | Path,
        message: str,
        *,
        row: int | None = None,
        column: str | None = None,
    ) -> None:
        self.path = str(path)
        self.row = row
        self.column = column

        location = Path(self.path).name
        if row is not None:
            location += f":{row}"
        if column is not None:
            location += f" [column {column!r}]"
        super().__init__(f"{location}: {message}")


@dataclass(frozen=True, slots=True)
class GameInfo:
    """A single game on the slate."""

    away_team: str
    home_team: str
    kickoff_utc: str | None

    @property
    def key(self) -> str:
        """Stable identifier, e.g. ``"KC@BUF"``."""
        return f"{self.away_team}@{self.home_team}"

    def opponent_of(self, team: str) -> str | None:
        if team == self.home_team:
            return self.away_team
        if team == self.away_team:
            return self.home_team
        return None


def parse_game_info(raw: str, *, path: str | Path, row: int) -> GameInfo:
    """Parse DraftKings' ``Game Info`` column.

    The time component is optional because DraftKings omits or mangles it for
    postponed and rescheduled games. A missing kickoff is recorded as ``None``
    rather than guessed -- the team matchup is still usable, and the
    two-distinct-games constraint depends on it.
    """
    match = _GAME_INFO_RE.match(raw or "")
    if not match:
        raise SlateSchemaError(
            path,
            f"could not parse game info {raw!r}; expected something like "
            f"'KC@BUF 09/13/2026 01:00PM ET'",
            row=row,
            column="Game Info",
        )

    kickoff_utc = None
    if match.group("date"):
        naive = datetime.strptime(
            f"{match.group('date')} {match.group('time')}", "%m/%d/%Y %I:%M%p"
        )
        # DraftKings labels these ET whether the date falls in EST or EDT;
        # ZoneInfo resolves which applies rather than assuming a fixed offset.
        eastern = naive.replace(tzinfo=DK_TIMEZONE)
        kickoff_utc = eastern.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return GameInfo(
        away_team=match.group("away").upper(),
        home_team=match.group("home").upper(),
        kickoff_utc=kickoff_utc,
    )


@dataclass(frozen=True, slots=True)
class SlatePlayer:
    """One draftable player or defense, normalized.

    ``source_player_id`` is DraftKings' own identifier. It is deliberately
    *not* resolved to an nflverse id here: capture must never fail because a
    name could not be matched, since slate data cannot be re-obtained after
    the fact. Resolution happens later, against the crosswalk.
    """

    source_player_id: str
    name: str
    position: str
    salary: int
    team: str
    game: GameInfo
    entity_type: str                       # 'player' or 'dst'
    roster_positions: tuple[str, ...] = ()
    avg_points_per_game: float | None = None

    #: Injury designation as DraftKings reports it: 'Q', 'IR', 'OUT', or
    #: None when the field is blank. Captured, never acted on here --
    #: whether to exclude a status is the optimizer's decision, and the
    #: status as it stood at capture time is point-in-time data itself.
    status: str | None = None

    # API-only metadata. None on the CSV path because the CSV cannot know it.
    draft_group_id: int | None = None
    lock_time_utc: str | None = None

    @property
    def opponent(self) -> str | None:
        return self.game.opponent_of(self.team)

    @property
    def is_defense(self) -> bool:
        return self.entity_type == "dst"

    @property
    def is_flagged(self) -> bool:
        """Whether DraftKings has any injury designation on this player."""
        return self.status is not None


class SalarySource(Protocol):
    """Anything that can produce a normalized slate.

    Both the CSV import and the future draftables adapter satisfy this, which
    is what allows the golden-file test to assert the two paths agree on every
    field the CSV is capable of expressing.
    """

    @property
    def source_name(self) -> str: ...

    def load(self) -> list[SlatePlayer]: ...
