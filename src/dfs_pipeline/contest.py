"""DraftKings NFL Classic contest rules, as data.

Every contest rule in this project lives here and nowhere else. Constants
scattered through call sites drift apart; a single module can be asserted
against in tests and audited in one screen.

Verification status
-------------------
The salary cap, roster shape, minimum-games rule and FLEX eligibility below
were cross-checked on 2026-08-17 against ``DimaKudosh/pydfs-lineup-optimizer``
(commit ``429db96``), an independently written optimizer that models the same
contest using explicit roster slots rather than aggregate position counts.
Both formulations admit exactly the same set of lineups; see DEVLOG.md for
the proof. That is corroboration between two implementations, *not* an
authority on DraftKings' current published rules.

Scoring constants are deliberately absent. They belong in one canonical
config alongside a fixture of real stat lines paired with published DK point
totals, and they must be verified against DraftKings directly rather than
carried over from an assumption. Until that verification happens, encoding
them here would look like knowledge we do not have.
"""

from __future__ import annotations

from collections.abc import Mapping
from itertools import product

__all__ = [
    "SALARY_CAP",
    "ROSTER_SIZE",
    "MIN_DISTINCT_GAMES",
    "SLOT_ORDER",
    "FLEX_ELIGIBLE",
    "POSITION_BOUNDS",
    "legal_roster_shapes",
    "is_legal_roster_shape",
    "InvalidRosterShape",
]

#: Maximum combined salary of the nine rostered players, in dollars.
SALARY_CAP = 50_000

#: Number of players in a DraftKings NFL Classic lineup.
ROSTER_SIZE = 9

#: A lineup must contain players from at least this many distinct games.
#: This prevents an entry from betting everything on a single game.
MIN_DISTINCT_GAMES = 2

#: The slot order DraftKings uses in its bulk-upload CSV header.
#:
#: VERIFIED 2026-08-17 against a real DraftKings upload template. See
#: :mod:`dfs_pipeline.upload` for the template's own instructions and the
#: golden test that pins this against a recorded copy.
SLOT_ORDER: tuple[str, ...] = (
    "QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST",
)

#: Positions the FLEX slot accepts. Notably excludes QB and DST.
FLEX_ELIGIBLE = frozenset({"RB", "WR", "TE"})

#: Inclusive (minimum, maximum) count for each position across the full
#: nine-player roster, i.e. *after* the FLEX slot has been filled.
#:
#: The ranges for RB, WR and TE are wider than their dedicated slot counts
#: precisely because FLEX can absorb one extra of any of the three.
POSITION_BOUNDS: Mapping[str, tuple[int, int]] = {
    "QB": (1, 1),
    "RB": (2, 3),
    "WR": (3, 4),
    "TE": (1, 2),
    "DST": (1, 1),
}


class InvalidRosterShape(ValueError):
    """Raised when a set of position counts is not a legal DK Classic roster.

    Carries the offending counts so the error message can name the actual
    problem rather than leaving the caller to guess.
    """

    def __init__(self, counts: Mapping[str, int], reason: str) -> None:
        self.counts = dict(counts)
        self.reason = reason
        super().__init__(f"{reason} (got {self.counts})")


def legal_roster_shapes() -> frozenset[tuple[tuple[str, int], ...]]:
    """Enumerate every position-count combination DraftKings accepts.

    Derived from :data:`POSITION_BOUNDS` and :data:`ROSTER_SIZE` rather than
    hardcoded, so the enumeration cannot drift out of sync with the bounds it
    is supposed to express.

    Returns exactly three shapes for DK NFL Classic: the FLEX slot resolves
    to one extra RB, WR, or TE.

    Each shape is a sorted tuple of ``(position, count)`` pairs, which makes
    it hashable and comparison-stable.
    """
    positions = sorted(POSITION_BOUNDS)
    ranges = [range(lo, hi + 1) for lo, hi in (POSITION_BOUNDS[p] for p in positions)]

    shapes = set()
    for combo in product(*ranges):
        if sum(combo) == ROSTER_SIZE:
            shapes.add(tuple(zip(positions, combo)))
    return frozenset(shapes)


def is_legal_roster_shape(counts: Mapping[str, int]) -> bool:
    """Return whether ``counts`` is a legal DK Classic roster shape.

    ``counts`` maps position to the number of players at that position across
    the whole lineup. Positions absent from the mapping are treated as zero,
    so ``{"QB": 1}`` is simply illegal rather than an error.
    """
    try:
        validate_roster_shape(counts)
    except InvalidRosterShape:
        return False
    return True


def validate_roster_shape(counts: Mapping[str, int]) -> None:
    """Raise :class:`InvalidRosterShape` if ``counts`` is not a legal roster.

    Prefer this over :func:`is_legal_roster_shape` on any path where a caller
    would otherwise have to invent its own error message. The whole point is
    that a rejection says *which* rule was broken.
    """
    unknown = set(counts) - set(POSITION_BOUNDS)
    if unknown:
        raise InvalidRosterShape(
            counts, f"unknown position(s): {', '.join(sorted(unknown))}"
        )

    if any(v < 0 for v in counts.values()):
        raise InvalidRosterShape(counts, "position counts cannot be negative")

    total = sum(counts.get(p, 0) for p in POSITION_BOUNDS)
    if total != ROSTER_SIZE:
        raise InvalidRosterShape(
            counts, f"roster must contain exactly {ROSTER_SIZE} players, got {total}"
        )

    for position, (lo, hi) in POSITION_BOUNDS.items():
        n = counts.get(position, 0)
        if not lo <= n <= hi:
            raise InvalidRosterShape(
                counts, f"{position} count must be between {lo} and {hi}, got {n}"
            )
