"""Projection CSV import, for whatever file the operator hands us.

Schema status: **VERIFIED 2026-08-17** against a real Daily Fantasy Fuel
export. DFF's actual columns are ``ppg_projection`` and
``ownership_projection`` -- neither of which resembles the obvious guess, and
the first draft of this reader rejected the file outright. That rejection was
the design working: it named every alias it looked for and every header it
found, which is what made the fix a one-line change instead of an
investigation.

Other vendors' spellings remain unverified but are retained as aliases.

Why the reader is flexible but the failure is strict
----------------------------------------------------
Projection vendors rename columns between seasons and between products, and a
weekly pipeline that breaks on a renamed header is a pipeline that misses a
slate. But a reader that silently guesses which column is the projection is
far worse: it would produce plausible numbers from the wrong field. So the
alias list is generous, the required set is small, and anything outside it is
an error that names the file and the headers actually present.

Identity
--------
Projections arrive keyed by **name**, never by a DraftKings id. Rows are given
a stable within-source key via :func:`~dfs_pipeline.names.normalize_name`, so
next week's capture lines up with this week's history. That key is explicitly
*not* a claim of cross-source identity -- resolving these names onto nflverse
ids is the crosswalk's job, and it needs team and position agreement plus a
human for the residue.

Capture never blocks on that resolution. An unmatchable projection is still
recorded, because a projection as it stood on Saturday cannot be re-obtained
on Monday.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from dfs_pipeline.adapters.base import SlateSchemaError
from dfs_pipeline.names import normalize_name

__all__ = [
    "ProjectionRow",
    "ProjectionsCsvAdapter",
    "NAME_COLUMNS",
    "PROJECTION_COLUMNS",
]

#: Accepted spellings for the player-name column, in preference order.
NAME_COLUMNS = (
    "name", "player", "player name", "playername", "full name", "player_name",
)

#: Sources that split the name instead of providing it whole.
FIRST_NAME_COLUMNS = ("first name", "first_name", "firstname")
LAST_NAME_COLUMNS = ("last name", "last_name", "lastname")

#: Accepted spellings for the projected-points column, in preference order.
#: DraftKings-specific spellings come first: a file carrying both DK and
#: FanDuel projections must not be read with the wrong one.
PROJECTION_COLUMNS = (
    "dk_projection", "dk projection", "dkprojection",
    # Daily Fantasy Fuel's actual column, VERIFIED 2026-08-17 against a real
    # export. Note `value_projection` is deliberately NOT here: it is points
    # per $1,000 of salary, not projected points, and reading it would produce
    # numbers around 3.0 that look entirely plausible.
    "ppg_projection",
    "projection", "proj", "fpts", "points", "projected points",
    "proj points", "projpoints", "fantasy points", "points proj",
)

OWNERSHIP_COLUMNS = (
    "ownership_projection",   # Daily Fantasy Fuel, VERIFIED 2026-08-17
    "dk_ownership", "dk ownership", "ownership", "own", "own%", "proj_own",
    "projected ownership", "roster%",
)
POSITION_COLUMNS = ("position", "pos", "dk_position")
TEAM_COLUMNS = ("team", "teamabbrev", "tm", "team_abbrev")
OPPONENT_COLUMNS = ("opponent", "opp", "opp_team")
SALARY_COLUMNS = ("dk_salary", "salary", "sal")
#: Columns stating when the projection was GENERATED -- a genuine
#: `effective_at`, distinct from when we read the file.
#:
#: Deliberately excludes `game_date` and `slate_date`. Those name the day the
#: games are played, not the moment the projection was computed. Daily Fantasy
#: Fuel carries `game_date` and it sits in the FUTURE relative to capture, so
#: treating it as `effective_at` would assert we knew Sunday's numbers weeks
#: early -- and the store's clock-skew CHECK would reject the row outright.
#: Verified 2026-08-17: a real DFF export states no computation time at all.
UPDATED_COLUMNS = ("updated", "last_updated", "timestamp", "generated_at")

#: Injury designation, where the source provides one.
INJURY_COLUMNS = ("injury_status", "injury", "status")


@dataclass(frozen=True, slots=True)
class ProjectionRow:
    """One projected player from one source."""

    name: str                   #: as the source spelled it, preserved verbatim
    normalized_name: str        #: stable within-source key
    projection: float
    subject_key: str            #: normalized name, disambiguated if necessary
    position: str | None = None
    team: str | None = None
    opponent: str | None = None
    ownership: float | None = None
    salary: int | None = None
    stated_effective_at: str | None = None
    injury_status: str | None = None


def _lookup(headers: dict[str, str], candidates: tuple[str, ...]) -> str | None:
    """Return the actual header matching the first candidate that is present."""
    for candidate in candidates:
        if candidate in headers:
            return headers[candidate]
    return None


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace("%", "").replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


class ProjectionsCsvAdapter:
    """Reads a projection CSV into normalized rows."""

    def __init__(self, path: str | Path, *, source_name: str) -> None:
        self.path = Path(path)
        if not source_name or not source_name.strip():
            raise ValueError("source_name is required (e.g. 'DFF', 'FANTASYPROS')")
        #: Each vendor is its own source, so their series never merge. Two
        #: vendors disagreeing is signal, and a consensus computed at capture
        #: time would destroy it.
        self.source_name = source_name.strip().upper()

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
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        if reader.fieldnames is None:
            raise SlateSchemaError(self.path, "file is empty; expected a header row")

        # Map lowercased header -> actual header, so lookups are spelling and
        # case tolerant without losing the original for error messages.
        headers = {h.strip().lower(): h for h in reader.fieldnames if h}

        name_col = _lookup(headers, NAME_COLUMNS)
        first_col = _lookup(headers, FIRST_NAME_COLUMNS)
        last_col = _lookup(headers, LAST_NAME_COLUMNS)
        if name_col is None and not (first_col and last_col):
            raise SlateSchemaError(
                self.path,
                f"no player-name column found. Looked for "
                f"{', '.join(NAME_COLUMNS)} (or a first/last name pair). "
                f"Found: {', '.join(sorted(headers.values()))}",
            )

        projection_col = _lookup(headers, PROJECTION_COLUMNS)
        if projection_col is None:
            raise SlateSchemaError(
                self.path,
                f"no projection column found. Looked for "
                f"{', '.join(PROJECTION_COLUMNS)}. "
                f"Found: {', '.join(sorted(headers.values()))}",
            )

        optional = {
            "position": _lookup(headers, POSITION_COLUMNS),
            "team": _lookup(headers, TEAM_COLUMNS),
            "opponent": _lookup(headers, OPPONENT_COLUMNS),
            "ownership": _lookup(headers, OWNERSHIP_COLUMNS),
            "salary": _lookup(headers, SALARY_COLUMNS),
            "updated": _lookup(headers, UPDATED_COLUMNS),
            "injury": _lookup(headers, INJURY_COLUMNS),
        }

        rows = []
        for line_no, record in enumerate(reader, start=2):
            parsed = self._parse_row(
                record, line_no, name_col, first_col, last_col,
                projection_col, optional,
            )
            if parsed is not None:
                rows.append(parsed)

        if not rows:
            raise SlateSchemaError(
                self.path, "no usable projection rows found below the header"
            )
        return self._disambiguate(rows)

    # -- internals ---------------------------------------------------------

    def _parse_row(
        self, record, line_no, name_col, first_col, last_col, projection_col, optional
    ) -> ProjectionRow | None:
        if not any((v or "").strip() for v in record.values()):
            return None  # trailing blank line

        if name_col:
            name = (record.get(name_col) or "").strip()
        else:
            name = " ".join(
                p for p in (
                    (record.get(first_col) or "").strip(),
                    (record.get(last_col) or "").strip(),
                ) if p
            )
        if not name:
            raise SlateSchemaError(
                self.path, "player name is empty", row=line_no,
                column=name_col or f"{first_col}/{last_col}",
            )

        projection = _to_float(record.get(projection_col))
        if projection is None:
            raise SlateSchemaError(
                self.path,
                f"projection {record.get(projection_col)!r} for {name!r} is not a number",
                row=line_no, column=projection_col,
            )

        normalized = normalize_name(name)
        if not normalized:
            raise SlateSchemaError(
                self.path,
                f"player name {name!r} normalizes to nothing usable",
                row=line_no, column=name_col or "name",
            )

        salary = _to_float(record.get(optional["salary"])) if optional["salary"] else None
        return ProjectionRow(
            name=name,
            normalized_name=normalized,
            subject_key=normalized,  # may be refined by _disambiguate
            projection=projection,
            position=(record.get(optional["position"]) or "").strip().upper() or None
            if optional["position"] else None,
            team=(record.get(optional["team"]) or "").strip().upper() or None
            if optional["team"] else None,
            opponent=(record.get(optional["opponent"]) or "").strip().upper() or None
            if optional["opponent"] else None,
            ownership=_to_float(record.get(optional["ownership"]))
            if optional["ownership"] else None,
            salary=int(salary) if salary is not None else None,
            stated_effective_at=(record.get(optional["updated"]) or "").strip() or None
            if optional["updated"] else None,
            injury_status=(record.get(optional["injury"]) or "").strip().upper() or None
            if optional["injury"] else None,
        )

    def _disambiguate(self, rows: list[ProjectionRow]) -> list[ProjectionRow]:
        """Give colliding names distinct keys, or refuse to guess.

        Two different players genuinely can share a name on one slate. Merging
        their projections would be silent, plausible corruption -- one player's
        number attached to the other's salary -- so where team and position
        cannot separate them, this raises instead.
        """
        buckets: dict[str, list[ProjectionRow]] = defaultdict(list)
        for row in rows:
            buckets[row.normalized_name].append(row)

        resolved: list[ProjectionRow] = []
        for normalized, group in buckets.items():
            if len(group) == 1:
                resolved.append(group[0])
                continue

            keyed = {}
            for row in group:
                parts = [normalized]
                if row.team:
                    parts.append(row.team)
                if row.position:
                    parts.append(row.position)
                keyed.setdefault("|".join(parts), []).append(row)

            if any(len(v) > 1 for v in keyed.values()):
                who = ", ".join(
                    f"{r.name} ({r.position or '?'} {r.team or '?'})" for r in group
                )
                raise SlateSchemaError(
                    self.path,
                    f"{len(group)} rows share the name key {normalized!r} and "
                    f"cannot be separated by team or position: {who}. Merging "
                    f"them would attach one player's projection to another.",
                )
            for key, [row] in keyed.items():
                resolved.append(
                    ProjectionRow(
                        name=row.name,
                        normalized_name=row.normalized_name,
                        subject_key=key,
                        projection=row.projection,
                        position=row.position,
                        team=row.team,
                        opponent=row.opponent,
                        ownership=row.ownership,
                        salary=row.salary,
                        stated_effective_at=row.stated_effective_at,
                        injury_status=row.injury_status,
                    )
                )
        return resolved
