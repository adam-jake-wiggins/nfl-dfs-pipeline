"""Tests for slot assignment.

The handoff's requirement is specific: prove `assign_slots()` correct over
every valid position multiset, and make an unfillable slot a hard error rather
than a blank cell. Both are done exhaustively below -- every legal DK Classic
roster shape, in every distinct player ordering.
"""

from __future__ import annotations

from collections import Counter

import pytest
from hypothesis import given, settings, strategies as st

from dfs_pipeline.contest import (
    FLEX_ELIGIBLE,
    ROSTER_SIZE,
    SLOT_ELIGIBILITY,
    SLOT_ORDER,
    legal_roster_shapes,
)
from dfs_pipeline.lineup import UnassignableLineup, assign_slots, slot_eligibility


class Player:
    """Minimal stand-in exposing the `.position` attribute the matcher reads."""

    __slots__ = ("name", "position")

    def __init__(self, name: str, position: str) -> None:
        self.name, self.position = name, position

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.name}:{self.position}"


def roster(positions):
    return [Player(f"p{i}", pos) for i, pos in enumerate(positions)]


def distinct_permutations(items):
    """Every distinct ordering of a multiset, without generating duplicates.

    itertools.permutations would emit 9! = 362,880 tuples per shape and then
    need deduplication; this yields only the ~10,080 that actually differ.
    """
    counts = sorted(Counter(items).items())

    def walk(remaining, current):
        # Base case is "every count is spent", not "the list is empty" --
        # entries stay in place with a count of zero.
        if all(count == 0 for _, count in remaining):
            yield tuple(current)
            return
        for index, (value, count) in enumerate(remaining):
            if count == 0:
                continue
            reduced = list(remaining)
            reduced[index] = (value, count - 1)
            yield from walk(reduced, current + [value])

    yield from walk(counts, [])


def positions_of(shape):
    """Expand a roster shape into a flat list of positions."""
    return [position for position, count in shape for _ in range(count)]


def is_valid(assignment, slot_order=SLOT_ORDER) -> bool:
    return all(
        player.position in SLOT_ELIGIBILITY[slot]
        for slot, player in zip(slot_order, assignment)
    )


# ---------------------------------------------------------------------------
# The handoff's requirement, exhaustively
# ---------------------------------------------------------------------------

def test_every_legal_shape_assigns_in_every_ordering():
    """The proof. All three legal shapes, all distinct player orderings.

    Roughly 30,000 assignments. Any ordering that failed would mean the matcher
    inherited the greedy version's order-dependence.
    """
    checked = 0
    for shape in legal_roster_shapes():
        for ordering in distinct_permutations(positions_of(shape)):
            assignment = assign_slots(roster(ordering))
            assert len(assignment) == ROSTER_SIZE
            assert is_valid(assignment), (shape, ordering)
            checked += 1
    assert checked > 10_000, f"expected exhaustive coverage, ran {checked}"


def test_every_legal_shape_uses_every_player_exactly_once():
    for shape in legal_roster_shapes():
        players = roster(positions_of(shape))
        assignment = assign_slots(players)
        assert sorted(map(id, assignment)) == sorted(map(id, players))


# ---------------------------------------------------------------------------
# The case greedy gets wrong
# ---------------------------------------------------------------------------

def test_flex_does_not_steal_a_slot_another_position_needs():
    """Slots [.. FLEX .. WR WR WR] with exactly three receivers.

    A greedy pass that filled FLEX first would take a WR and leave two
    receivers for three WR slots -- failing on a lineup that plainly assigns.
    """
    players = roster(["WR", "WR", "WR", "RB", "QB", "RB", "TE", "DST", "WR"])
    assignment = assign_slots(players)
    assert is_valid(assignment)
    flex = assignment[SLOT_ORDER.index("FLEX")]
    assert flex.position in FLEX_ELIGIBLE


def test_a_second_tight_end_lands_in_flex():
    players = roster(["QB", "RB", "RB", "WR", "WR", "WR", "TE", "TE", "DST"])
    assignment = assign_slots(players)
    assert is_valid(assignment)
    assert assignment[SLOT_ORDER.index("FLEX")].position == "TE"


def test_a_third_running_back_lands_in_flex():
    players = roster(["QB", "RB", "RB", "RB", "WR", "WR", "WR", "TE", "DST"])
    assert assign_slots(players)[SLOT_ORDER.index("FLEX")].position == "RB"


def test_ordering_the_flex_player_first_still_works():
    """The strongest form of the ordering test: the surplus player comes first."""
    players = roster(["TE", "QB", "RB", "RB", "WR", "WR", "WR", "TE", "DST"])
    assert is_valid(assign_slots(players))


# ---------------------------------------------------------------------------
# Unassignable lineups raise; they never return a blank
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "positions, fragment",
    [
        (["QB"] * 9, "no player is eligible for the RB slot"),
        (["QB", "QB", "RB", "RB", "WR", "WR", "WR", "TE", "DST"],
         "no assignment of these players"),
        (["QB", "RB", "RB", "WR", "WR", "WR", "TE", "DST", "DST"],
         "no assignment of these players"),
        (["QB", "RB", "RB", "WR", "WR", "WR", "WR", "WR", "DST"],
         "no player is eligible for the TE slot"),
    ],
)
def test_impossible_rosters_raise(positions, fragment):
    with pytest.raises(UnassignableLineup, match=fragment):
        assign_slots(roster(positions))


def test_nothing_partial_is_ever_returned():
    """A blank cell uploads as broken while looking complete."""
    with pytest.raises(UnassignableLineup):
        assign_slots(roster(["QB"] * 9))


def test_the_error_names_the_positions_involved():
    with pytest.raises(UnassignableLineup) as exc:
        assign_slots(roster(["QB", "QB", "RB", "RB", "WR", "WR", "WR", "TE", "DST"]))
    assert "QB, QB" in str(exc.value)
    assert exc.value.positions.count("QB") == 2


def test_wrong_player_count_raises():
    with pytest.raises(UnassignableLineup, match="8 players but there are 9"):
        assign_slots(roster(["QB", "RB", "RB", "WR", "WR", "WR", "TE", "DST"]))


def test_an_unknown_slot_raises():
    with pytest.raises(UnassignableLineup, match="unknown roster slot"):
        slot_eligibility("KICKER")


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_the_same_lineup_always_assigns_the_same_way():
    """An optimizer emitting a different file each run is not reproducible."""
    players = roster(["QB", "RB", "RB", "WR", "WR", "WR", "TE", "TE", "DST"])
    first = assign_slots(players)
    for _ in range(20):
        assert [id(p) for p in assign_slots(players)] == [id(p) for p in first]


def test_reordering_the_input_may_change_the_output_but_stays_valid():
    """Determinism is per-input, not order-invariant -- and both are fine."""
    base = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "TE", "DST"]
    for ordering in list(distinct_permutations(base))[:200]:
        assert is_valid(assign_slots(roster(ordering)))


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------

def test_a_custom_position_accessor_is_supported():
    """Callers holding dicts or tuples should not have to wrap them."""
    players = [{"pos": p} for p in
               ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "RB", "DST"]]
    assignment = assign_slots(players, position_of=lambda p: p["pos"])
    assert assignment[0]["pos"] == "QB"
    assert assignment[-1]["pos"] == "DST"


def test_positions_are_matched_case_insensitively():
    players = roster(["qb", "rb", "Rb", "wr", "WR", "wR", "te", "rb", "dst"])
    assert len(assign_slots(players)) == ROSTER_SIZE


def test_real_slate_players_work_directly():
    """SlatePlayer exposes `.position`, so no accessor is needed."""
    from dfs_pipeline.adapters.base import GameInfo, SlatePlayer

    def make(position):
        return SlatePlayer(
            source_player_id="1", name="x", position=position, salary=3000,
            team="KC", game=GameInfo("KC", "BUF", None),
            entity_type="dst" if position == "DST" else "player",
        )

    lineup = [make(p) for p in
              ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "WR", "DST"]]
    assert is_valid(assign_slots(lineup))


@given(
    positions=st.lists(
        st.sampled_from(["QB", "RB", "WR", "TE", "DST"]),
        min_size=ROSTER_SIZE, max_size=ROSTER_SIZE,
    )
)
@settings(max_examples=400)
def test_any_nine_positions_either_assign_validly_or_raise(positions):
    """Never a partial result, never a wrong one, whatever arrives."""
    try:
        assignment = assign_slots(roster(positions))
    except UnassignableLineup:
        return
    assert len(assignment) == ROSTER_SIZE
    assert is_valid(assignment)
    assert len({id(p) for p in assignment}) == ROSTER_SIZE


@given(
    positions=st.lists(
        st.sampled_from(["QB", "RB", "WR", "TE", "DST"]),
        min_size=ROSTER_SIZE, max_size=ROSTER_SIZE,
    )
)
@settings(max_examples=300)
def test_assignability_agrees_with_the_legal_shape_rules(positions):
    """The matcher and the contest rules must never disagree.

    Two independent statements of the same constraint: one enumerates legal
    shapes arithmetically, the other solves a matching problem.
    """
    from dfs_pipeline.contest import is_legal_roster_shape

    counts = Counter(p for p in positions)
    legal = is_legal_roster_shape(dict(counts))
    try:
        assign_slots(roster(positions))
        assignable = True
    except UnassignableLineup:
        assignable = False
    assert assignable == legal, (dict(counts), legal, assignable)
