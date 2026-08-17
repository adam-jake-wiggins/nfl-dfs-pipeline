"""Tests for the DraftKings NFL Classic domain rules.

These tests are the executable form of the contest rules. If DraftKings
changes a rule, or if someone edits a constant carelessly, these fail.
"""

from __future__ import annotations

import pytest
from hypothesis import given, strategies as st

from dfs_pipeline import contest
from dfs_pipeline.contest import (
    FLEX_ELIGIBLE,
    InvalidRosterShape,
    MIN_DISTINCT_GAMES,
    POSITION_BOUNDS,
    ROSTER_SIZE,
    SALARY_CAP,
    SLOT_ORDER,
    is_legal_roster_shape,
    legal_roster_shapes,
    validate_roster_shape,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_salary_cap_is_fifty_thousand():
    """Cross-checked against pydfs-lineup-optimizer (commit 429db96)."""
    assert SALARY_CAP == 50_000


def test_roster_size_is_nine():
    assert ROSTER_SIZE == 9
    assert len(SLOT_ORDER) == ROSTER_SIZE


def test_minimum_two_distinct_games():
    """DK forbids loading a lineup entirely into one game."""
    assert MIN_DISTINCT_GAMES == 2


def test_flex_excludes_quarterback_and_defense():
    """The FLEX slot takes RB/WR/TE only -- never a second QB or DST."""
    assert FLEX_ELIGIBLE == {"RB", "WR", "TE"}
    assert "QB" not in FLEX_ELIGIBLE
    assert "DST" not in FLEX_ELIGIBLE


def test_slot_order_matches_dedicated_slot_counts():
    """SLOT_ORDER and POSITION_BOUNDS must describe the same contest.

    Each position's dedicated slot count has to equal its lower bound: the
    bounds' upper end is higher only because FLEX can absorb one more.
    """
    for position, (lower, _upper) in POSITION_BOUNDS.items():
        assert SLOT_ORDER.count(position) == lower, (
            f"{position} appears {SLOT_ORDER.count(position)} times in "
            f"SLOT_ORDER but has a lower bound of {lower}"
        )
    assert SLOT_ORDER.count("FLEX") == 1


def test_flex_upper_bounds_exceed_lower_by_exactly_one():
    """Only FLEX-eligible positions have any slack, and only one slot of it."""
    for position, (lower, upper) in POSITION_BOUNDS.items():
        expected_slack = 1 if position in FLEX_ELIGIBLE else 0
        assert upper - lower == expected_slack, (
            f"{position} has slack {upper - lower}, expected {expected_slack}"
        )


# ---------------------------------------------------------------------------
# Legal roster shapes
# ---------------------------------------------------------------------------

def test_exactly_three_legal_shapes():
    """DK NFL Classic admits precisely three position-count combinations.

    This is the enumeration behind the equivalence proof in DEVLOG.md: with
    QB and DST pinned at 1, the remaining seven slots distribute across
    RB/WR/TE with exactly one floating.
    """
    assert len(legal_roster_shapes()) == 3


def test_the_three_shapes_are_the_expected_ones():
    expected = {
        # FLEX used on a third RB
        (("DST", 1), ("QB", 1), ("RB", 3), ("TE", 1), ("WR", 3)),
        # FLEX used on a fourth WR
        (("DST", 1), ("QB", 1), ("RB", 2), ("TE", 1), ("WR", 4)),
        # FLEX used on a second TE
        (("DST", 1), ("QB", 1), ("RB", 2), ("TE", 2), ("WR", 3)),
    }
    assert legal_roster_shapes() == expected


def test_every_legal_shape_has_nine_players():
    for shape in legal_roster_shapes():
        assert sum(count for _, count in shape) == ROSTER_SIZE


def test_qb_and_dst_are_pinned_in_every_legal_shape():
    """No legal lineup carries two QBs or two defenses."""
    for shape in legal_roster_shapes():
        counts = dict(shape)
        assert counts["QB"] == 1
        assert counts["DST"] == 1


# ---------------------------------------------------------------------------
# Validation, and the specificity of its failures
# ---------------------------------------------------------------------------

def test_canonical_lineup_is_legal():
    assert is_legal_roster_shape(
        {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "DST": 1, }
    ) is False, "eight players is not a legal roster"

    assert is_legal_roster_shape(
        {"QB": 1, "RB": 2, "WR": 4, "TE": 1, "DST": 1}
    ) is True


@pytest.mark.parametrize(
    "counts, fragment",
    [
        ({"QB": 1, "RB": 2, "WR": 3, "TE": 1, "DST": 1}, "exactly 9 players"),
        ({"QB": 2, "RB": 2, "WR": 3, "TE": 1, "DST": 1}, "QB count must be between 1 and 1"),
        ({"QB": 1, "RB": 4, "WR": 2, "TE": 1, "DST": 1}, "RB count must be between 2 and 3"),
        ({"QB": 1, "RB": 2, "WR": 3, "TE": 2, "DST": 1, "K": 0}, "unknown position"),
        ({"QB": 1, "RB": -1, "WR": 3, "TE": 1, "DST": 1}, "cannot be negative"),
    ],
)
def test_invalid_shapes_fail_with_a_specific_message(counts, fragment):
    """A rejection must name the rule that was broken.

    'Invalid lineup' tells a user nothing. Every failure path here has to
    identify the actual problem, which is what makes the error actionable.
    """
    with pytest.raises(InvalidRosterShape) as exc:
        validate_roster_shape(counts)
    assert fragment in str(exc.value)


def test_exception_carries_the_offending_counts():
    counts = {"QB": 3, "RB": 2, "WR": 3, "TE": 1, "DST": 1}
    with pytest.raises(InvalidRosterShape) as exc:
        validate_roster_shape(counts)
    assert exc.value.counts == counts


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------

_position_counts = st.fixed_dictionaries(
    {p: st.integers(min_value=0, max_value=6) for p in POSITION_BOUNDS}
)


@given(counts=_position_counts)
def test_legality_agrees_with_the_enumeration(counts):
    """The predicate and the enumeration must never disagree.

    Two independent implementations of the same rule: one checks bounds
    arithmetically, the other enumerates by brute force. Hypothesis generates
    thousands of position-count combinations looking for a case where they
    diverge.
    """
    predicate_says = is_legal_roster_shape(counts)
    enumeration_says = tuple(sorted(counts.items())) in legal_roster_shapes()
    assert predicate_says == enumeration_says


@given(counts=_position_counts)
def test_validation_never_raises_anything_but_invalid_roster_shape(counts):
    """No arbitrary input should produce a raw traceback.

    Malformed input must fail as a typed, catchable domain error -- never as
    a KeyError or TypeError leaking out of the internals.
    """
    try:
        validate_roster_shape(counts)
    except InvalidRosterShape:
        pass  # expected for illegal inputs


def test_module_exports_match_dunder_all():
    """Everything advertised in __all__ actually exists."""
    for name in contest.__all__:
        assert hasattr(contest, name), f"__all__ advertises missing name: {name}"
