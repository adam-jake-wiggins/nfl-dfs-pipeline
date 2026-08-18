"""Lineup construction as a mixed-integer program, in two formulations.

Sequential vs simultaneous
==========================
Building N lineups admits two designs.

**Sequential** solves N times, each solve forbidding overlap with what came
before and zeroing out players who have hit their exposure cap. It is what the
prototype does and what ``pydfs-lineup-optimizer`` does. The objection is that
it is a heuristic: each solve is optimal given the earlier ones, but the *set*
is not jointly optimal.

**Simultaneous** puts all N lineups in one program -- variables ``x[l][i]``,
roster and salary constraints repeated per lineup, exposure caps as sums
across lineups, and pairwise overlap limits. That set is provably optimal.

The catch is symmetry. Lineups are interchangeable, so every solution has N!
equivalent relabellings and branch-and-bound explores them all. Pairwise
uniqueness also grows as N², which at 150 lineups is over eleven thousand
constraints across a 500-player pool.

Both are implemented here so the choice rests on measured runtime rather than
on assertion. See DEVLOG.md for the numbers.

Shortfalls are loud
===================
When fewer lineups are produced than requested, the run says so and diagnoses
*which* constraint bound, by relaxing each optional constraint in turn and
reporting which relaxation restores feasibility. "Requested 20, produced 11"
without a reason is not an actionable message.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

import pulp

from dfs_pipeline.contest import (
    MIN_DISTINCT_GAMES,
    POSITION_BOUNDS,
    ROSTER_SIZE,
    SALARY_CAP,
)
from dfs_pipeline.lineup import assign_slots

__all__ = [
    "BRINGBACK_POSITIONS",
    "lock_shortfalls",
    "position_shortfalls",
    "OptimizerReport",
    "STACK_POSITIONS",
    "Settings",
    "optimize",
    "optimize_simultaneous",
]

#: Positions that count toward a QB stack. Receivers only: a running back on
#: the QB's own team correlates weakly, and often negatively -- a team that
#: runs the ball is a team not throwing it.
STACK_POSITIONS = frozenset({"WR", "TE"})

#: Positions that count as a bring-back from the opposing team. Running backs
#: are included here because the correlation being bought is *game total*
#: rather than passing volume: a shootout lifts everyone on both sides.
#:
#: The prototype used exactly these two sets but never said why they differed,
#: which made the asymmetry look like an oversight. It is a real distinction
#: and is now stated.
BRINGBACK_POSITIONS = frozenset({"WR", "TE", "RB"})

log = logging.getLogger("dfs_pipeline.optimizer")


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything that shapes a lineup set."""

    lineups: int = 1
    salary_cap: int = SALARY_CAP
    min_salary: int = 0
    min_unique: int = 1
    max_exposure: float = 1.0
    stack: int = 0
    bringback: int = 0
    #: Source player ids forced into every lineup. Ids rather than names:
    #: the prototype locked by name, so a lock on a duplicated name forced
    #: "exactly one player called that" rather than the intended person.
    locks: tuple[str, ...] = ()
    min_games: int = MIN_DISTINCT_GAMES
    time_limit: float | None = None


@dataclass
class OptimizerReport:
    """What the solve did, including what it could not do."""

    requested: int = 0
    produced: int = 0
    mode: str = "sequential"
    seconds: float = 0.0
    binding_constraint: str | None = None
    per_lineup_seconds: list[float] = field(default_factory=list)

    @property
    def short(self) -> bool:
        return self.produced < self.requested

    def render(self) -> str:
        lines = [
            f"optimizer: {self.produced}/{self.requested} lineups "
            f"({self.mode}, {self.seconds:.2f}s)"
        ]
        if self.short:
            lines.append(
                f"  WARNING: requested {self.requested}, produced {self.produced}"
            )
            lines.append(
                f"  binding constraint: {self.binding_constraint or 'undiagnosed'}"
            )
        return "\n".join(lines)


def _build(pool: Sequence, settings: Settings, suffix: str = ""):
    """The per-lineup constraint set, shared by both formulations."""
    problem_vars = {
        i: pulp.LpVariable(f"x{suffix}_{i}", cat="Binary") for i in range(len(pool))
    }
    constraints = []

    def by_position(position):
        return [i for i, p in enumerate(pool) if p.position == position]

    constraints.append(
        ("roster size", pulp.lpSum(problem_vars.values()) == ROSTER_SIZE)
    )
    for position, (low, high) in POSITION_BOUNDS.items():
        indices = by_position(position)
        constraints.append(
            (f"{position} minimum",
             pulp.lpSum(problem_vars[i] for i in indices) >= low)
        )
        constraints.append(
            (f"{position} maximum",
             pulp.lpSum(problem_vars[i] for i in indices) <= high)
        )

    if settings.stack or settings.bringback:
        constraints.extend(
            _correlation_constraints(pool, problem_vars, settings)
        )

    constraints.append(
        ("salary cap",
         pulp.lpSum(pool[i].salary * problem_vars[i] for i in problem_vars)
         <= settings.salary_cap)
    )
    if settings.min_salary:
        constraints.append(
            ("minimum salary",
             pulp.lpSum(pool[i].salary * problem_vars[i] for i in problem_vars)
             >= settings.min_salary)
        )
    return problem_vars, constraints


def _correlation_constraints(pool, problem_vars, settings):
    """QB stacking and bring-back, plus the QB-versus-own-DST exclusion.

    Each is written per quarterback and gated on that quarterback being
    selected, so the constraint is inert unless he is in the lineup.
    """
    built = []
    quarterbacks = [i for i, p in enumerate(pool) if p.position == "QB"]

    for qb in quarterbacks:
        team = pool[qb].team
        opponent = pool[qb].opponent

        if settings.stack:
            mates = [
                i for i, p in enumerate(pool)
                if p.team == team and p.position in STACK_POSITIONS
            ]
            built.append((
                f"stack for {pool[qb].name}",
                pulp.lpSum(problem_vars[i] for i in mates)
                >= settings.stack * problem_vars[qb],
            ))

        if settings.bringback and opponent:
            opposing = [
                i for i, p in enumerate(pool)
                if p.team == opponent and p.position in BRINGBACK_POSITIONS
            ]
            if opposing:
                built.append((
                    f"bring-back for {pool[qb].name}",
                    pulp.lpSum(problem_vars[i] for i in opposing)
                    >= settings.bringback * problem_vars[qb],
                ))

        # Never pair a quarterback with the defense playing against him: the
        # correlation is strongly negative by construction.
        for dst in (i for i, p in enumerate(pool) if p.position == "DST"):
            if pool[dst].team and pool[dst].team == opponent:
                built.append((
                    f"{pool[qb].name} vs opposing DST",
                    problem_vars[qb] + problem_vars[dst] <= 1,
                ))

    return built


def _game_constraints(pool, problem_vars, settings, suffix=""):
    """At least N distinct games, via one indicator per game."""
    games = {}
    for i, player in enumerate(pool):
        games.setdefault(player.game.key, []).append(i)

    indicators = {
        key: pulp.LpVariable(f"g{suffix}_{n}", cat="Binary")
        for n, key in enumerate(games)
    }
    built = []
    for key, indices in games.items():
        built.append(
            (f"game {key} indicator",
             indicators[key] <= pulp.lpSum(problem_vars[i] for i in indices))
        )
    built.append(
        ("minimum distinct games",
         pulp.lpSum(indicators.values()) >= settings.min_games)
    )
    return built


def position_shortfalls(pool: Sequence) -> list[str]:
    """Positions the pool cannot fill, checked before any solve runs.

    A pool missing an entire position makes every lineup infeasible, and the
    solver reports only "infeasible" -- which is true and useless. This is a
    realistic failure rather than a theoretical one: FantasyPros ships defenses
    in a separate file, and several projection sources omit them entirely, so a
    pool built by joining a slate to projections can silently arrive with zero
    DSTs.
    """
    available = Counter(p.position for p in pool)
    return [
        f"{position}: {available.get(position, 0)} in the pool, {low} required"
        for position, (low, _high) in POSITION_BOUNDS.items()
        if available.get(position, 0) < low
    ]


def lock_shortfalls(pool: Sequence, locks: Sequence[str]) -> str | None:
    """Check locks are satisfiable before solving, or say why not.

    Three ways locks fail, each with a message naming the cause rather than
    leaving the solver to report a bare infeasibility:

    * a locked player is not in the pool -- usually filtered out by status,
      which is worth saying plainly since the operator asked for them by name;
    * more locks than roster slots;
    * locks that already violate a position bound, e.g. two quarterbacks.
    """
    if not locks:
        return None

    available = {p.source_player_id: p for p in pool}
    missing = [lock for lock in locks if lock not in available]
    if missing:
        return (
            f"locked player(s) not in the pool: {', '.join(missing)}. They may "
            f"have been excluded by an injury status or a salary floor."
        )

    if len(locks) > ROSTER_SIZE:
        return f"{len(locks)} locks but only {ROSTER_SIZE} roster slots"

    counts = Counter(available[lock].position for lock in locks)
    for position, count in counts.items():
        _low, high = POSITION_BOUNDS.get(position, (0, ROSTER_SIZE))
        if count > high:
            return (
                f"{count} locked {position}s but a lineup admits at most {high}"
            )
    return None


def optimize(pool: Sequence, settings: Settings, *, projection_of=None):
    """Build lineups sequentially. Returns (lineups, report)."""
    projection_of = projection_of or (lambda p: getattr(p, "projection", 0.0))
    report = OptimizerReport(requested=settings.lineups, mode="sequential")
    started = time.perf_counter()

    lock_problem = lock_shortfalls(pool, settings.locks)
    if lock_problem:
        report.binding_constraint = lock_problem
        report.seconds = time.perf_counter() - started
        return [], report

    shortfalls = position_shortfalls(pool)
    if shortfalls:
        report.binding_constraint = (
            "the player pool cannot fill every roster position -- "
            + "; ".join(shortfalls)
        )
        report.seconds = time.perf_counter() - started
        return [], report

    lineups: list[list] = []
    previous: list[list[int]] = []
    usage: dict[str, int] = {}
    cap = max(1, int(settings.max_exposure * settings.lineups))

    for _ in range(settings.lineups):
        loop_started = time.perf_counter()
        problem = pulp.LpProblem("dk", pulp.LpMaximize)
        variables, constraints = _build(pool, settings)
        problem += pulp.lpSum(
            projection_of(pool[i]) * variables[i] for i in variables
        )
        for _, constraint in constraints:
            problem += constraint
        for _, constraint in _game_constraints(pool, variables, settings):
            problem += constraint

        if settings.max_exposure < 1.0:
            for i, player in enumerate(pool):
                if usage.get(player.source_player_id, 0) >= cap:
                    problem += variables[i] == 0

        for i, player in enumerate(pool):
            if player.source_player_id in settings.locks:
                problem += variables[i] == 1

        for earlier in previous:
            problem += (
                pulp.lpSum(variables[i] for i in earlier)
                <= ROSTER_SIZE - settings.min_unique
            )

        status = problem.solve(
            pulp.PULP_CBC_CMD(msg=0, timeLimit=settings.time_limit)
        )
        report.per_lineup_seconds.append(time.perf_counter() - loop_started)

        if pulp.LpStatus[status] != "Optimal":
            report.binding_constraint = _diagnose(pool, settings, previous, usage, cap)
            break

        chosen = [i for i in variables if variables[i].value() and variables[i].value() > 0.5]
        previous.append(chosen)
        for i in chosen:
            key = pool[i].source_player_id
            usage[key] = usage.get(key, 0) + 1
        lineups.append([pool[i] for i in chosen])

    report.produced = len(lineups)
    report.seconds = time.perf_counter() - started
    return lineups, report


def optimize_simultaneous(pool: Sequence, settings: Settings, *, projection_of=None):
    """Build all lineups in one program. Provably optimal as a set."""
    projection_of = projection_of or (lambda p: getattr(p, "projection", 0.0))
    report = OptimizerReport(requested=settings.lineups, mode="simultaneous")
    started = time.perf_counter()

    problem = pulp.LpProblem("dk_simultaneous", pulp.LpMaximize)
    per_lineup = []
    objective = []

    for l in range(settings.lineups):
        variables, constraints = _build(pool, settings, suffix=f"_{l}")
        for _, constraint in constraints:
            problem += constraint
        for _, constraint in _game_constraints(pool, variables, settings, suffix=f"_{l}"):
            problem += constraint
        per_lineup.append(variables)
        objective.extend(
            projection_of(pool[i]) * variables[i] for i in variables
        )

    problem += pulp.lpSum(objective)

    # Exposure across the whole set -- the constraint sequential can only
    # approximate, because it does not know what later lineups will want.
    if settings.max_exposure < 1.0:
        cap = max(1, int(settings.max_exposure * settings.lineups))
        for i in range(len(pool)):
            problem += pulp.lpSum(v[i] for v in per_lineup) <= cap

    # Pairwise uniqueness: O(N^2) constraints.
    for a in range(settings.lineups):
        for b in range(a + 1, settings.lineups):
            # Overlap between two lineups is sum_i min(x_a_i, x_b_i), which is
            # not linear. The standard linearisation adds one auxiliary binary
            # per (pair, player) bounded below both. That term is what makes
            # this formulation expensive: it grows as N^2 * |pool|.
            if settings.min_unique > 0:
                shared = [
                    pulp.LpVariable(f"s_{a}_{b}_{i}", cat="Binary")
                    for i in range(len(pool))
                ]
                for i, s in enumerate(shared):
                    problem += s <= per_lineup[a][i]
                    problem += s <= per_lineup[b][i]
                problem += (
                    pulp.lpSum(shared) <= ROSTER_SIZE - settings.min_unique
                )

    status = problem.solve(pulp.PULP_CBC_CMD(msg=0, timeLimit=settings.time_limit))
    report.seconds = time.perf_counter() - started

    if pulp.LpStatus[status] not in ("Optimal", "Not Solved"):
        report.binding_constraint = f"solver returned {pulp.LpStatus[status]}"
        report.produced = 0
        return [], report

    lineups = []
    for variables in per_lineup:
        chosen = [
            i for i in variables
            if variables[i].value() and variables[i].value() > 0.5
        ]
        if len(chosen) == ROSTER_SIZE:
            lineups.append([pool[i] for i in chosen])

    report.produced = len(lineups)
    if report.short:
        report.binding_constraint = (
            "solver did not return a complete set within the time limit"
        )
    return lineups, report


def _diagnose(pool, settings, previous, usage, cap) -> str:
    """Name the constraint that made the next lineup infeasible.

    Relaxes each optional constraint in turn and reports the first whose
    removal restores feasibility. "Requested 20, produced 11" without a reason
    is not actionable.
    """
    # dataclasses.replace, not **__dict__: Settings uses slots, so it has no
    # __dict__ at all.
    candidates = [
        ("min_unique", lambda s: dataclasses.replace(s, min_unique=0)),
        ("max_exposure", lambda s: dataclasses.replace(s, max_exposure=1.0)),
        ("min_salary", lambda s: dataclasses.replace(s, min_salary=0)),
    ]
    for name, relax in candidates:
        relaxed = relax(settings)
        problem = pulp.LpProblem("probe", pulp.LpMaximize)
        variables, constraints = _build(pool, relaxed)
        problem += 0
        for _, constraint in constraints:
            problem += constraint
        for _, constraint in _game_constraints(pool, variables, relaxed):
            problem += constraint
        if relaxed.max_exposure < 1.0:
            for i, player in enumerate(pool):
                if usage.get(player.source_player_id, 0) >= cap:
                    problem += variables[i] == 0
        if relaxed.min_unique > 0:
            for earlier in previous:
                problem += (
                    pulp.lpSum(variables[i] for i in earlier)
                    <= ROSTER_SIZE - relaxed.min_unique
                )
        if pulp.LpStatus[problem.solve(pulp.PULP_CBC_CMD(msg=0))] == "Optimal":
            return f"{name} (relaxing it restores feasibility)"
    return "player pool too small or too constrained for another distinct lineup"
