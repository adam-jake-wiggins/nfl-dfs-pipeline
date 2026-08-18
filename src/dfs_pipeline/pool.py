"""Player-pool filtering: deciding who is eligible before the solver runs.

Where this belongs
==================
Filtering is an **optimizer** concern, never a capture concern. Capture records
a player's status as it stood and moves on; that designation is point-in-time
data which cannot be recovered later, and discarding it at ingest would throw
away the very thing a backtest needs. So nothing here is wired into
``dfs-snapshot``. It runs when a lineup is being built, against whatever the
store already holds.

Status vocabularies disagree
============================
Sources spell the same designation differently, verified against real files on
2026-08-17:

======================  ==========================================
source                  values observed
======================  ==========================================
DraftKings salary CSV   ``Q``, ``IR``, ``OUT`` (blank when clear)
DraftKings draftables   ``Q``, ``IR``, ``OUT``, ``None`` (a string)
Daily Fantasy Fuel      ``O`` for out
======================  ==========================================

Note what is **absent**: DraftKings does not emit ``DOUBTFUL``, though the
handoff's example filter assumed it. A filter written to that spelling would
have matched nothing and silently left every doubtful player in the pool. So
inputs are normalized to a canonical vocabulary and an unrecognised status is
reported rather than ignored.

Absent status data is stated, not assumed
=========================================
If a slate carries no status column at all, that is not the same as every
player being healthy, and the report says so explicitly. Treating "we do not
know" as "everyone is fine" is the silent-degradation failure this project
exists to avoid.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

__all__ = [
    "CANONICAL_STATUSES",
    "DEFAULT_EXCLUDED",
    "PoolReport",
    "STATUS_ALIASES",
    "filter_pool",
    "normalize_status",
    "parse_status_list",
]

#: The canonical vocabulary everything is mapped onto.
CANONICAL_STATUSES = (
    "OUT",           # ruled out
    "DOUBTFUL",      # unlikely to play
    "QUESTIONABLE",  # game-time decision
    "IR",            # injured reserve
    "PUP",           # physically unable to perform
    "SUSPENDED",
    "PROBABLE",      # largely retired by the NFL, still seen in older feeds
)

#: Every spelling seen in the wild, mapped onto the canonical vocabulary.
#: DraftKings uses single letters; other sources spell them out.
STATUS_ALIASES: dict[str, str] = {
    "O": "OUT", "OUT": "OUT",
    "D": "DOUBTFUL", "DOUBTFUL": "DOUBTFUL",
    "Q": "QUESTIONABLE", "QUESTIONABLE": "QUESTIONABLE",
    "IR": "IR", "INJURED RESERVE": "IR", "IR-R": "IR",
    "PUP": "PUP",
    "SUS": "SUSPENDED", "SUSP": "SUSPENDED", "SUSPENDED": "SUSPENDED",
    "P": "PROBABLE", "PROBABLE": "PROBABLE",
}

#: Excluded unless a caller says otherwise: players who will not play at all.
#: QUESTIONABLE is deliberately **not** here -- a questionable player is a
#: judgement call and often the point of a contrarian lineup, so removing them
#: by default would quietly make that decision for the operator.
DEFAULT_EXCLUDED = frozenset({"OUT", "IR", "PUP", "SUSPENDED"})

#: DraftKings writes this string for a clear player; it is not a designation.
_NOT_A_STATUS = frozenset({"", "NONE", "ACTIVE", "ACT", "A", "-", "--"})


def normalize_status(value: str | None) -> str | None:
    """Map a source's status onto the canonical vocabulary.

    Returns ``None`` for a clear player. Raises nothing: an unrecognised
    status is returned upper-cased so the caller can report it rather than
    have it silently vanish into "healthy".
    """
    if value is None:
        return None
    text = str(value).strip().upper()
    if text in _NOT_A_STATUS:
        return None
    return STATUS_ALIASES.get(text, text)


def parse_status_list(raw: str | Iterable[str] | None) -> frozenset[str]:
    """Parse ``--exclude-status OUT,DOUBTFUL`` into canonical statuses."""
    if raw is None:
        return frozenset()
    parts = raw.split(",") if isinstance(raw, str) else list(raw)
    resolved = set()
    for part in parts:
        canonical = normalize_status(part)
        if canonical:
            resolved.add(canonical)
    return frozenset(resolved)


@dataclass
class PoolReport:
    """What filtering did, and what it could not know."""

    considered: int = 0
    kept: int = 0
    excluded_by_status: dict[str, list[str]] = field(default_factory=dict)
    excluded_by_salary: list[str] = field(default_factory=list)
    excluded_by_name: list[str] = field(default_factory=list)
    unknown_statuses: dict[str, list[str]] = field(default_factory=dict)
    status_data_present: bool = True

    @property
    def excluded(self) -> int:
        return self.considered - self.kept

    def render(self) -> str:
        lines = [f"pool: {self.kept}/{self.considered} eligible"]

        if not self.status_data_present:
            # "We do not know" is not "everyone is healthy".
            lines.append(
                "  WARNING: no status data on this slate -- no injury filtering "
                "was possible, and nobody was excluded on that basis"
            )

        for status in sorted(self.excluded_by_status):
            names = self.excluded_by_status[status]
            lines.append(f"  excluded {status:<13} {len(names):>4}")
            for name in names[:5]:
                lines.append(f"    {name}")
            if len(names) > 5:
                lines.append(f"    ... and {len(names) - 5} more")

        if self.excluded_by_salary:
            lines.append(f"  excluded below salary floor {len(self.excluded_by_salary):>4}")
        if self.excluded_by_name:
            lines.append(f"  excluded by name            {len(self.excluded_by_name):>4}")

        if self.unknown_statuses:
            lines.append(
                f"  WARNING: {len(self.unknown_statuses)} unrecognised status "
                f"value(s); nobody was filtered on them:"
            )
            for status, names in sorted(self.unknown_statuses.items()):
                lines.append(f"    {status!r}: {len(names)} player(s), e.g. {names[0]}")

        return "\n".join(lines)


def filter_pool(
    players: Sequence,
    *,
    exclude_statuses: Iterable[str] | str | None = None,
    min_salary: int | None = None,
    exclude_names: Iterable[str] | None = None,
    status_of: Callable[[object], str | None] = lambda p: getattr(p, "status", None),
    name_of: Callable[[object], str] = lambda p: getattr(p, "name", ""),
    salary_of: Callable[[object], int] = lambda p: getattr(p, "salary", 0),
) -> tuple[list, PoolReport]:
    """Filter a slate into an eligible pool, with a report of what was removed.

    ``exclude_statuses`` defaults to :data:`DEFAULT_EXCLUDED`; pass an empty
    iterable to disable status filtering entirely.

    Every exclusion is named in the report. A pool that silently shrinks is
    indistinguishable from a pool that was always small, and the difference
    matters when a lineup turns out to be missing the player you expected.
    """
    excluded = (
        DEFAULT_EXCLUDED
        if exclude_statuses is None
        else parse_status_list(exclude_statuses)
    )
    banned = {n.strip().casefold() for n in (exclude_names or []) if n.strip()}

    report = PoolReport(considered=len(players))
    report.status_data_present = any(status_of(p) for p in players)

    kept = []
    for player in players:
        name = name_of(player)

        if banned and name.strip().casefold() in banned:
            report.excluded_by_name.append(name)
            continue

        if min_salary is not None and salary_of(player) < min_salary:
            report.excluded_by_salary.append(name)
            continue

        status = normalize_status(status_of(player))
        if status is not None and status not in CANONICAL_STATUSES:
            # Unrecognised: report it, but do not act on it. Guessing that an
            # unknown code means "out" could silently empty a position.
            report.unknown_statuses.setdefault(status, []).append(name)
        elif status is not None and status in excluded:
            report.excluded_by_status.setdefault(status, []).append(name)
            continue

        kept.append(player)

    report.kept = len(kept)
    return kept, report
