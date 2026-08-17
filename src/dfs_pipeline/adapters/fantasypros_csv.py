"""FantasyPros projection exports, re-scored at DraftKings rules.

Schema VERIFIED 2026-08-17 against real per-position exports.

Three properties of these files drive the whole design.

**1. Column names repeat, so parsing is positional.**
FantasyPros lays a two-phase stat line into one header row::

    Player,Team,ATT,CMP,YDS,TDS,INTS,ATT,YDS,TDS,FL,FPTS
                     ^^^          ^^^  ^^^
                     passing      rushing

``csv.DictReader`` silently keeps the *last* duplicate, so asking it for
``YDS`` returns Jalen Hurts' 27.3 rushing yards when the passing value is
217.5. Every layout below is therefore declared by index and the header is
verified exactly before any row is read: a changed header is a loud failure,
never a silent misread.

**2. Their FPTS column is half-PPR. DraftKings is full PPR.**
Verified against their own component stats: every running back's FPTS matches
``base + 0.5 * receptions`` to within rounding. Taking that column as a
DraftKings projection would under-project every pass-catcher by half a point
per reception -- three-plus points for a busy receiver, entirely plausible and
entirely wrong.

So the FPTS column is **ignored**. Their projected *stat lines* are scored with
:mod:`dfs_pipeline.scoring`, the same canonical rules applied to realized
nflverse results. That fixes the scoring basis exactly rather than by
adjustment, and it means one definition of DraftKings scoring governs
everything in this project.

**3. The same player can appear in two position files.**
Verified: Connor Heyward, Riley Nowakowski and Max Bredeson each appear in
both the RB and TE exports with different projections -- FantasyPros is
projecting two distinct usages, and both numbers are real. Subject keys
therefore carry team and position, so the two never collide.

**4. These are season-long per-game averages, not weekly projections.**
Hurts is projected at 19.9 whether he faces the best or worst pass defense.
That is a useful prior; it is not a slate projection, and filing it as one
would be exactly the plausible-looking corruption this project guards against.
They are therefore stored under a **distinct metric**,
``projection_season_avg_dk_points``.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

from dfs_pipeline.adapters.base import SlateSchemaError
from dfs_pipeline.adapters.projections_csv import ProjectionRow
from dfs_pipeline.names import name_key, normalize_name
from dfs_pipeline.scoring import (
    DefenseStats,
    OffenseStats,
    score_defense,
    score_offense,
)
from dfs_pipeline.teams import UnknownTeam, resolve_team

__all__ = [
    "FantasyProsCsvAdapter",
    "LAYOUTS",
    "SEASON_AVERAGE_METRIC",
]

#: Season per-game averages are a different quantity from a weekly slate
#: projection and must never share a metric name with one.
SEASON_AVERAGE_METRIC = "projection_season_avg_dk_points"


@dataclass(frozen=True, slots=True)
class Layout:
    """One position's exact column layout, verified before use."""

    position: str
    header: tuple[str, ...]
    fields: dict[str, int]

    def check(self, actual: list[str], path) -> None:
        cleaned = tuple(h.strip() for h in actual)
        if cleaned != self.header:
            raise SlateSchemaError(
                path,
                f"unexpected {self.position} header. FantasyPros repeats column "
                f"names, so this file is parsed by position and the header must "
                f"match exactly.\n  expected: {','.join(self.header)}\n  "
                f"found:    {','.join(cleaned)}",
            )


LAYOUTS: dict[str, Layout] = {
    "QB": Layout(
        "QB",
        ("Player", "Team", "ATT", "CMP", "YDS", "TDS", "INTS",
         "ATT", "YDS", "TDS", "FL", "FPTS"),
        {"passing_yards": 4, "passing_tds": 5, "interceptions_thrown": 6,
         "rushing_yards": 8, "rushing_tds": 9, "fumbles_lost": 10},
    ),
    "RB": Layout(
        "RB",
        ("Player", "Team", "ATT", "YDS", "TDS", "REC", "YDS", "TDS", "FL", "FPTS"),
        {"rushing_yards": 3, "rushing_tds": 4, "receptions": 5,
         "receiving_yards": 6, "receiving_tds": 7, "fumbles_lost": 8},
    ),
    # Note the order differs from RB: receiving comes first for a receiver.
    "WR": Layout(
        "WR",
        ("Player", "Team", "REC", "YDS", "TDS", "ATT", "YDS", "TDS", "FL", "FPTS"),
        {"receptions": 2, "receiving_yards": 3, "receiving_tds": 4,
         "rushing_yards": 6, "rushing_tds": 7, "fumbles_lost": 8},
    ),
    "TE": Layout(
        "TE",
        ("Player", "Team", "REC", "YDS", "TDS", "FL", "FPTS"),
        {"receptions": 2, "receiving_yards": 3, "receiving_tds": 4,
         "fumbles_lost": 5},
    ),
    "DST": Layout(
        "DST",
        ("Player", "Team", "SACK", "INT", "FR", "FF", "TD", "SAFETY",
         "PA", "YDS_AGN", "FPTS"),
        {"sacks": 2, "interceptions": 3, "fumble_recoveries": 4,
         "interception_return_tds": 6, "safeties": 7, "points_allowed": 8},
    ),
}

#: Exports we deliberately refuse, with the reason surfaced to the operator.
REFUSED = {
    "K": "DraftKings NFL Classic has no kicker slot, so kicker projections "
         "cannot appear in a lineup.",
    "FLX": "The FLEX export duplicates the RB, WR and TE files. Ingesting both "
           "would record two projections for every flex-eligible player.",
}


def _num(value: str | None) -> float:
    if value is None:
        return 0.0
    text = value.strip().replace(",", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


class FantasyProsCsvAdapter:
    """Reads one FantasyPros per-position export and scores it at DK rules."""

    source_name = "FANTASYPROS"
    metric_name = SEASON_AVERAGE_METRIC

    def __init__(self, path: str | Path, *, position: str) -> None:
        self.path = Path(path)
        key = position.strip().upper()
        if key in REFUSED:
            raise SlateSchemaError(self.path, f"{key} export refused: {REFUSED[key]}")
        if key not in LAYOUTS:
            raise SlateSchemaError(
                self.path,
                f"unknown position {key!r}. Supported: {', '.join(sorted(LAYOUTS))}.",
            )
        self.position = key
        self.layout = LAYOUTS[key]

    # -- public API --------------------------------------------------------

    def raw_bytes(self) -> bytes:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            raise SlateSchemaError(self.path, "file does not exist") from None
        except IsADirectoryError:
            raise SlateSchemaError(self.path, "path is a directory, not a file") from None

    def load(self) -> list[ProjectionRow]:
        return self.loads(self.raw_bytes())

    def loads(self, raw: bytes) -> list[ProjectionRow]:
        table = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
        if not table:
            raise SlateSchemaError(self.path, "file is empty; expected a header row")

        self.layout.check(table[0], self.path)

        rows: list[ProjectionRow] = []
        for line_no, record in enumerate(table[1:], start=2):
            parsed = self._parse_row(record, line_no)
            if parsed is not None:
                rows.append(parsed)

        if not rows:
            raise SlateSchemaError(
                self.path, "no usable projection rows found below the header"
            )
        return rows

    # -- internals ---------------------------------------------------------

    def _parse_row(self, record: list[str], line_no: int) -> ProjectionRow | None:
        # FantasyPros emits a short spacer row of blanks after the header.
        if len(record) < len(self.layout.header):
            if not any(cell.strip() for cell in record):
                return None
            raise SlateSchemaError(
                self.path,
                f"row has {len(record)} fields, expected "
                f"{len(self.layout.header)}",
                row=line_no,
            )

        name = record[0].strip()
        if not name:
            return None

        team = record[1].strip().upper() or None
        get = lambda field: _num(record[self.layout.fields[field]])  # noqa: E731

        if self.position == "DST":
            return self._parse_defense(name, line_no, get)

        stats = OffenseStats(
            passing_yards=get("passing_yards") if "passing_yards" in self.layout.fields else 0.0,
            passing_tds=get("passing_tds") if "passing_tds" in self.layout.fields else 0,
            interceptions_thrown=(
                get("interceptions_thrown")
                if "interceptions_thrown" in self.layout.fields else 0
            ),
            rushing_yards=get("rushing_yards") if "rushing_yards" in self.layout.fields else 0.0,
            rushing_tds=get("rushing_tds") if "rushing_tds" in self.layout.fields else 0,
            receptions=get("receptions") if "receptions" in self.layout.fields else 0,
            receiving_yards=(
                get("receiving_yards") if "receiving_yards" in self.layout.fields else 0.0
            ),
            receiving_tds=get("receiving_tds") if "receiving_tds" in self.layout.fields else 0,
            fumbles_lost=get("fumbles_lost") if "fumbles_lost" in self.layout.fields else 0,
        )
        return ProjectionRow(
            name=name,
            normalized_name=normalize_name(name),
            # Position is part of the key because FantasyPros exports one file
            # per position and the same human can appear in two of them.
            # Verified 2026-08-17: Connor Heyward, Riley Nowakowski and Max
            # Bredeson each appear in both the RB and TE exports with DIFFERENT
            # projections -- FantasyPros is projecting two different usages,
            # and both numbers are real. Keying on name alone would make one
            # silently overwrite the other.
            subject_key=name_key(name, team, self.position),
            projection=score_offense(stats),
            position=self.position,
            team=team,
        )

    def _parse_defense(self, name: str, line_no: int, get) -> ProjectionRow:
        """Score a defense, with one honest caveat about the points-allowed tier.

        DraftKings scores points allowed as a **step function**: 0, 1-6, 7-13,
        and so on. FantasyPros projects an *average* points allowed -- 16.8 for
        Jacksonville. Pushing an average through a step function is not the
        same as averaging the steps: a defense expected to allow 16.8 has real
        probability of landing in the 7-13 band (+4) or the 21-27 band (0),
        and the expected score is not the score of the expectation.

        The tier of the mean is used here because it is the only defensible
        point estimate available from a mean, but it is an approximation and
        is recorded as one. Doing better needs a distribution, which is the
        research roadmap's territory, not capture's.
        """
        try:
            team = resolve_team(name)
        except UnknownTeam as exc:
            raise SlateSchemaError(
                self.path, f"could not resolve defense {name!r}: {exc}", row=line_no
            ) from None

        stats = DefenseStats(
            sacks=get("sacks"),
            interceptions=int(round(get("interceptions"))),
            fumble_recoveries=int(round(get("fumble_recoveries"))),
            interception_return_tds=int(round(get("interception_return_tds"))),
            safeties=int(round(get("safeties"))),
            points_allowed=int(round(get("points_allowed"))),
        )
        return ProjectionRow(
            name=team.nickname,
            normalized_name=normalize_name(team.nickname),
            subject_key=name_key(team.nickname, team.abbrev, "DST"),
            projection=score_defense(stats),
            position="DST",
            team=team.abbrev,
        )
