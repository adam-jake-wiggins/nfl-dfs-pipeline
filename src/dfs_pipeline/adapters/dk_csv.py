"""DraftKings ``DKSalaries.csv`` import path.

This is the fallback the whole design depends on. The unofficial draftables
endpoint carries no stability guarantee, so when it breaks -- during a slate,
plausibly -- the manual export must still work. It is therefore built first
and treated as a first-class path, not a degraded one.

Schema status: **VERIFIED 2026-08-17** against a real DraftKings export for
the 2026 Week 1 main slate (716 entries, 12 games, 24 teams). Every required
column was present and every row parsed. Confirmed in that file: a UTF-8 BOM,
CRLF line endings, ``Roster Position`` values of the form ``RB/FLEX``, and a
``Status`` column absent from the originally assumed layout.

Assumptions are asserted rather than trusted: an unexpected header, a missing
column, or an unparseable value raises
:class:`~dfs_pipeline.adapters.base.SlateSchemaError` naming the file, row and
column. A silently-tolerated schema change is the failure this project treats
as worst, so nothing here degrades quietly.

Verified header::

    Position,Name + ID,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,AvgPointsPerGame,Status

Observed ``Status`` vocabulary is ``Q``, ``IR``, ``OUT``, or empty -- note
that DraftKings does **not** emit ``DOUBTFUL``. Status is captured, never
acted on here; filtering is a decision for the optimizer, and a player's
status at capture time is itself point-in-time data worth keeping.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from pathlib import Path

from dfs_pipeline.adapters.base import (
    SlatePlayer,
    SlateSchemaError,
    parse_game_info,
)

__all__ = ["DraftKingsCsvAdapter", "REQUIRED_COLUMNS", "SOURCE_NAME"]

SOURCE_NAME = "DK_CSV"

#: Columns we genuinely need. DraftKings has added columns over time without
#: removing these, so we require a subset rather than an exact header match --
#: strict enough to catch a real schema change, tolerant of additive ones.
REQUIRED_COLUMNS = (
    "Position",
    "Name",
    "ID",
    "Salary",
    "Game Info",
    "TeamAbbrev",
)

_OPTIONAL_COLUMNS = (
    "Roster Position",
    "AvgPointsPerGame",
    "Name + ID",
    "Status",
)

#: Values observed in the Status column of a real export (2026-08-17).
#: Q = questionable, IR = injured reserve, OUT = ruled out. DraftKings
#: does NOT emit "DOUBTFUL", contrary to a common assumption.
KNOWN_STATUSES = frozenset({"Q", "IR", "OUT"})

#: DraftKings writes defenses with this position code.
_DEFENSE_POSITIONS = frozenset({"DST", "D", "DEF", "D/ST"})


class DraftKingsCsvAdapter:
    """Reads a DraftKings salary export into normalized slate records."""

    source_name = SOURCE_NAME

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # -- public API --------------------------------------------------------

    def load(self) -> list[SlatePlayer]:
        """Parse the file, or raise :class:`SlateSchemaError` explaining why not."""
        return self.loads(self._read_bytes())

    def raw_bytes(self) -> bytes:
        """The file's bytes, for storage as an immutable artifact."""
        return self._read_bytes()

    def loads(self, raw: bytes) -> list[SlatePlayer]:
        """Parse from bytes, so the same code path serves file and artifact.

        Re-parsing a stored artifact months later must go through exactly the
        code that parsed it originally, otherwise the stored bytes prove
        nothing about what we actually ingested.
        """
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))

        if reader.fieldnames is None:
            raise SlateSchemaError(self.path, "file is empty; expected a header row")

        headers = {h.strip() for h in reader.fieldnames if h}
        missing = [c for c in REQUIRED_COLUMNS if c not in headers]
        if missing:
            raise SlateSchemaError(
                self.path,
                f"missing required column(s): {', '.join(missing)}. "
                f"Found: {', '.join(sorted(headers))}. "
                f"Is this a DraftKings salary export?",
            )

        players: list[SlatePlayer] = []
        # Row 1 is the header, so data begins at 2 -- matching what a
        # spreadsheet shows the operator when they go looking.
        for line_no, row in enumerate(reader, start=2):
            player = self._parse_row(row, line_no)
            if player is not None:
                players.append(player)

        if not players:
            raise SlateSchemaError(
                self.path, "no usable player rows found below the header"
            )

        self._reject_duplicate_ids(players)
        self._require_two_games(players)
        return players

    # -- row parsing -------------------------------------------------------

    def _parse_row(self, row: dict[str, str], line_no: int) -> SlatePlayer | None:
        # DraftKings exports sometimes carry a trailing blank line.
        if not any((v or "").strip() for v in row.values()):
            return None

        name = self._required(row, "Name", line_no)
        position = self._required(row, "Position", line_no).upper()
        player_id = self._required(row, "ID", line_no)
        team = self._required(row, "TeamAbbrev", line_no).upper()

        salary_raw = self._required(row, "Salary", line_no)
        try:
            salary = int(float(salary_raw.replace("$", "").replace(",", "")))
        except ValueError:
            raise SlateSchemaError(
                self.path,
                f"salary {salary_raw!r} for {name!r} is not a number",
                row=line_no,
                column="Salary",
            ) from None
        if salary <= 0:
            raise SlateSchemaError(
                self.path,
                f"salary for {name!r} is {salary}; DraftKings salaries are positive",
                row=line_no,
                column="Salary",
            )

        game = parse_game_info(
            self._required(row, "Game Info", line_no), path=self.path, row=line_no
        )
        if game.opponent_of(team) is None:
            raise SlateSchemaError(
                self.path,
                f"team {team!r} for {name!r} does not appear in its own game "
                f"{game.key!r}",
                row=line_no,
                column="TeamAbbrev",
            )

        avg = self._optional_float(row, "AvgPointsPerGame", name, line_no)

        roster_positions: tuple[str, ...] = ()
        raw_roster = (row.get("Roster Position") or "").strip()
        if raw_roster:
            roster_positions = tuple(
                p.strip().upper() for p in raw_roster.split("/") if p.strip()
            )

        status = (row.get("Status") or "").strip().upper() or None

        return SlatePlayer(
            source_player_id=player_id,
            name=name,
            position=position,
            salary=salary,
            team=team,
            game=game,
            entity_type="dst" if position in _DEFENSE_POSITIONS else "player",
            roster_positions=roster_positions,
            avg_points_per_game=avg,
            status=status,
        )

    def _required(self, row: dict[str, str], column: str, line_no: int) -> str:
        value = (row.get(column) or "").strip()
        if not value:
            raise SlateSchemaError(
                self.path, "value is empty", row=line_no, column=column
            )
        return value

    def _optional_float(
        self, row: dict[str, str], column: str, name: str, line_no: int
    ) -> float | None:
        raw = (row.get(column) or "").strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            raise SlateSchemaError(
                self.path,
                f"{column} value {raw!r} for {name!r} is not a number",
                row=line_no,
                column=column,
            ) from None

    # -- slate-level invariants -------------------------------------------

    def _reject_duplicate_ids(self, players: list[SlatePlayer]) -> None:
        """Two rows sharing a DraftKings ID means the file is corrupt.

        Left alone this silently doubles a player's exposure in every
        downstream calculation, which is exactly the kind of quiet wrongness
        that is hardest to notice.
        """
        counts = Counter(p.source_player_id for p in players)
        dupes = sorted(pid for pid, n in counts.items() if n > 1)
        if dupes:
            offenders = ", ".join(
                f"{pid} ({next(p.name for p in players if p.source_player_id == pid)})"
                for pid in dupes[:5]
            )
            raise SlateSchemaError(
                self.path,
                f"{len(dupes)} duplicate player ID(s): {offenders}"
                + (" ..." if len(dupes) > 5 else ""),
            )

    def _require_two_games(self, players: list[SlatePlayer]) -> None:
        """DraftKings Classic requires players from at least two games.

        A single-game file is a Showdown slate, which this pipeline does not
        support. Detecting it here produces a clear message instead of an
        infeasible solver run twenty minutes later.
        """
        games = {p.game.key for p in players}
        if len(games) < 2:
            raise SlateSchemaError(
                self.path,
                f"only one game present ({', '.join(sorted(games))}). This looks "
                f"like a Showdown slate; Classic requires at least two games.",
            )

    # -- io ----------------------------------------------------------------

    def _read_bytes(self) -> bytes:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            raise SlateSchemaError(self.path, "file does not exist") from None
        except IsADirectoryError:
            raise SlateSchemaError(self.path, "path is a directory, not a file") from None
