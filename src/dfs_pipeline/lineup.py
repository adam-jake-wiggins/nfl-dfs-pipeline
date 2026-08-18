"""Assigning nine players to nine DraftKings roster slots.

Why this is not a greedy pass
=============================
The prototype filled each slot in order, taking the first eligible player it
found, and returned ``None`` into any slot it could not fill -- which the CSV
writer then emitted as a blank cell, producing a file that looks complete and
uploads as broken.

The deeper problem is that **greedy assignment is order-dependent and can fail
on assignable lineups**. Consider slots ``[FLEX, WR, WR, WR]`` and players
``[WR, WR, WR, RB]``. Fill FLEX first, take a WR, and only two receivers
remain for three WR slots -- failure, despite an obviously valid assignment
(RB into FLEX). The prototype avoided this only because it happened to fill
dedicated slots before FLEX. That is correct by luck of the constraint
structure, not by construction, and a future change -- a double-TE rule, a
Showdown variant, a new slot -- silently breaks it.

This module solves the actual problem: **maximum bipartite matching** between
players and slots. Kuhn's augmenting-path algorithm finds a perfect matching
if and only if one exists, so an assignable lineup is always assigned and an
unassignable one always raises. No ordering assumption, no luck.

At nine players the algorithm is instant; the reason to use it is correctness,
not speed.

Determinism
===========
A lineup can have several valid assignments -- with two tight ends, either may
take the TE slot and the other FLEX. Slots are processed in
:data:`~dfs_pipeline.contest.SLOT_ORDER` and players are tried in input order,
so the same lineup always produces the same assignment. Runs must be
reproducible, and an optimizer that emits a different file each time it is run
on identical input is not.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TypeVar

from dfs_pipeline.contest import ROSTER_SIZE, SLOT_ELIGIBILITY, SLOT_ORDER

__all__ = ["UnassignableLineup", "assign_slots", "slot_eligibility"]

T = TypeVar("T")


class UnassignableLineup(ValueError):
    """Raised when no valid slot assignment exists for a lineup.

    Carries the positions involved, because "cannot assign" without saying
    what was on the roster is not an actionable message.
    """

    def __init__(self, positions: Sequence[str], reason: str) -> None:
        self.positions = list(positions)
        self.reason = reason
        super().__init__(f"{reason} (positions: {', '.join(self.positions)})")


def slot_eligibility(slot: str) -> frozenset[str]:
    """Positions a named slot accepts."""
    try:
        return SLOT_ELIGIBILITY[slot]
    except KeyError:
        raise UnassignableLineup([], f"unknown roster slot {slot!r}") from None


def assign_slots(
    players: Sequence[T],
    *,
    position_of: Callable[[T], str] = lambda p: p.position,
    slot_order: Sequence[str] = SLOT_ORDER,
) -> tuple[T, ...]:
    """Assign players to slots, returning them in slot order.

    Raises :class:`UnassignableLineup` when no assignment exists. It never
    returns a partial result: a blank cell in an upload file is worse than a
    refusal, because the refusal is visible.

    ``position_of`` defaults to reading ``.position``, so
    :class:`~dfs_pipeline.adapters.base.SlatePlayer` works directly, while a
    caller holding dicts or tuples can supply its own accessor.
    """
    if len(players) != len(slot_order):
        raise UnassignableLineup(
            [position_of(p) for p in players],
            f"lineup has {len(players)} players but there are "
            f"{len(slot_order)} slots",
        )

    positions = [str(position_of(p)).strip().upper() for p in players]
    eligible = [slot_eligibility(slot) for slot in slot_order]

    # Which players each slot could take. Built once; the matcher walks it.
    candidates: list[list[int]] = [
        [i for i, position in enumerate(positions) if position in accepted]
        for accepted in eligible
    ]

    for slot, options in zip(slot_order, candidates):
        if not options:
            raise UnassignableLineup(
                positions,
                f"no player is eligible for the {slot} slot",
            )

    assignment = _match(candidates, len(players))
    if assignment is None:
        raise UnassignableLineup(
            positions,
            "no assignment of these players to the roster slots exists",
        )

    return tuple(players[index] for index in assignment)


def _match(candidates: list[list[int]], player_count: int) -> list[int] | None:
    """Kuhn's algorithm: a perfect matching of slots to players, or ``None``.

    ``candidates[s]`` lists the players slot ``s`` accepts. Returns a list
    giving the player index for each slot.

    The augmenting-path search is what makes this correct where greedy is not:
    when a slot finds its preferred player already taken, it asks that slot to
    move rather than giving up. Players are visited in index order so the
    result is deterministic.
    """
    player_to_slot: list[int | None] = [None] * player_count

    def augment(slot: int, seen: list[bool]) -> bool:
        for player in candidates[slot]:
            if seen[player]:
                continue
            seen[player] = True
            holder = player_to_slot[player]
            if holder is None or augment(holder, seen):
                player_to_slot[player] = slot
                return True
        return False

    for slot in range(len(candidates)):
        if not augment(slot, [False] * player_count):
            return None

    assignment: list[int | None] = [None] * len(candidates)
    for player, slot in enumerate(player_to_slot):
        if slot is not None:
            assignment[slot] = player

    # Unreachable: the loop above returns None the moment any slot fails to
    # augment, so reaching here means every slot holds a player. Kept as a
    # guard against a future edit that changes the loop's exit condition.
    if any(index is None for index in assignment):  # pragma: no cover
        return None
    return [index for index in assignment if index is not None]
