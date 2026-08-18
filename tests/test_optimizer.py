"""Tests for lineup construction.

The acceptance criterion is that **every lineup is provably DK-legal by test**,
so legality is checked structurally on every lineup every formulation produces
-- roster shape, salary cap, distinct games, no duplicate players -- rather
than spot-checked.

A small synthetic pool keeps the suite fast. The realistic-scale behaviour is
measured in the benchmark recorded in DEVLOG.md, not here: a test suite that
takes four minutes is a test suite people stop running.
"""

from __future__ import annotations

from collections import Counter

import pytest

from dfs_pipeline.adapters.base import GameInfo, SlatePlayer
from dfs_pipeline.contest import (
    MIN_DISTINCT_GAMES,
    ROSTER_SIZE,
    SALARY_CAP,
    is_legal_roster_shape,
)
from dfs_pipeline.lineup import assign_slots
from dfs_pipeline.optimizer import Settings, optimize, optimize_simultaneous


def make_pool(per_position=6, games=3):
    """A pool deep enough for many distinct lineups but small enough to be fast."""
    pool, n = [], 0
    matchups = [("KC", "BUF"), ("DAL", "PHI"), ("SF", "LAR"),
                ("NYG", "NYJ"), ("MIA", "NE")][:games]
    for away, home in matchups:
        game = GameInfo(away, home, "2026-09-13T17:00:00Z")
        for team in (away, home):
            for position in ("QB", "RB", "WR", "TE", "DST"):
                for k in range(per_position):
                    n += 1
                    pool.append(SlatePlayer(
                        source_player_id=str(n),
                        name=f"{team}-{position}-{k}",
                        position=position,
                        salary=3000 + (k * 700) % 6000,
                        team=team, game=game,
                        entity_type="dst" if position == "DST" else "player",
                    ))
    return pool


POOL = make_pool()
PROJECTIONS = {p.source_player_id: 5.0 + (int(p.source_player_id) % 17)
               for p in POOL}
project = lambda p: PROJECTIONS[p.source_player_id]


def assert_legal(lineup):
    """Every DraftKings Classic rule, checked structurally."""
    assert len(lineup) == ROSTER_SIZE
    ids = [p.source_player_id for p in lineup]
    assert len(set(ids)) == ROSTER_SIZE, "a player appears twice"
    assert is_legal_roster_shape(dict(Counter(p.position for p in lineup)))
    assert sum(p.salary for p in lineup) <= SALARY_CAP
    assert len({p.game.key for p in lineup}) >= MIN_DISTINCT_GAMES
    assign_slots(lineup)  # raises if the lineup cannot fill the roster slots


# ---------------------------------------------------------------------------
# Legality -- the acceptance criterion
# ---------------------------------------------------------------------------

def test_a_single_lineup_is_legal():
    lineups, report = optimize(POOL, Settings(lineups=1), projection_of=project)
    assert report.produced == 1
    assert_legal(lineups[0])


def test_every_lineup_in_a_set_is_legal():
    lineups, report = optimize(
        POOL, Settings(lineups=10, min_unique=1), projection_of=project
    )
    assert report.produced == 10
    for lineup in lineups:
        assert_legal(lineup)


@pytest.mark.slow
def test_the_simultaneous_formulation_also_produces_legal_lineups():
    lineups, report = optimize_simultaneous(
        POOL, Settings(lineups=2, min_unique=1, time_limit=60),
        projection_of=project,
    )
    assert report.produced == 2
    for lineup in lineups:
        assert_legal(lineup)


def test_lineups_never_exceed_the_salary_cap():
    lineups, _ = optimize(POOL, Settings(lineups=5), projection_of=project)
    for lineup in lineups:
        assert sum(p.salary for p in lineup) <= SALARY_CAP


def test_a_salary_floor_is_respected():
    floor = 40000
    lineups, _ = optimize(
        POOL, Settings(lineups=3, min_salary=floor), projection_of=project
    )
    assert lineups
    for lineup in lineups:
        assert floor <= sum(p.salary for p in lineup) <= SALARY_CAP


def test_lineups_span_at_least_two_games():
    lineups, _ = optimize(POOL, Settings(lineups=5), projection_of=project)
    for lineup in lineups:
        assert len({p.game.key for p in lineup}) >= MIN_DISTINCT_GAMES


# ---------------------------------------------------------------------------
# Set-level constraints
# ---------------------------------------------------------------------------

def test_lineups_differ_by_the_requested_minimum():
    lineups, _ = optimize(
        POOL, Settings(lineups=6, min_unique=3), projection_of=project
    )
    assert len(lineups) == 6
    for a in range(len(lineups)):
        for b in range(a + 1, len(lineups)):
            shared = {p.source_player_id for p in lineups[a]} & {
                p.source_player_id for p in lineups[b]
            }
            assert len(shared) <= ROSTER_SIZE - 3


def test_exposure_caps_are_respected():
    lineups, _ = optimize(
        POOL, Settings(lineups=10, max_exposure=0.4), projection_of=project
    )
    counts = Counter(
        p.source_player_id for lineup in lineups for p in lineup
    )
    assert max(counts.values()) <= 4, "0.4 of 10 lineups is 4"


def test_the_objective_is_actually_maximised():
    """The best available lineup should beat an arbitrary legal one."""
    lineups, _ = optimize(POOL, Settings(lineups=1), projection_of=project)
    best = sum(project(p) for p in lineups[0])

    naive = []
    for position, count in (("QB", 1), ("RB", 2), ("WR", 3), ("TE", 1), ("DST", 1)):
        naive += [p for p in POOL if p.position == position][:count]
    naive += [p for p in POOL if p.position == "RB" and p not in naive][:1]
    assert best >= sum(project(p) for p in naive[:ROSTER_SIZE])


# ---------------------------------------------------------------------------
# Shortfalls are loud and diagnosed
# ---------------------------------------------------------------------------

def test_an_impossible_request_reports_a_shortfall():
    """"Requested 20, produced 11" without a reason is not actionable."""
    tiny = make_pool(per_position=1, games=2)
    lineups, report = optimize(
        tiny, Settings(lineups=25, min_unique=9), projection_of=project
    )
    assert report.short
    assert report.produced < 25
    assert report.binding_constraint
    assert "WARNING" in report.render()


def test_the_binding_constraint_is_named():
    tiny = make_pool(per_position=1, games=2)
    _, report = optimize(
        tiny, Settings(lineups=20, min_unique=9), projection_of=project
    )
    assert any(
        term in report.binding_constraint
        for term in ("min_unique", "max_exposure", "min_salary", "player pool")
    ), report.binding_constraint


def test_an_unsatisfiable_salary_floor_is_diagnosed():
    _, report = optimize(
        POOL, Settings(lineups=2, min_salary=SALARY_CAP + 5000),
        projection_of=project,
    )
    assert report.produced == 0
    assert report.binding_constraint


def test_a_report_with_no_shortfall_does_not_warn():
    _, report = optimize(POOL, Settings(lineups=2), projection_of=project)
    assert not report.short
    assert "WARNING" not in report.render()


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------

def test_the_same_inputs_produce_the_same_lineups():
    """An optimizer emitting a different set each run is not reproducible,
    and reproducibility is an acceptance criterion."""
    first, _ = optimize(POOL, Settings(lineups=4, min_unique=2),
                        projection_of=project)
    for _ in range(2):
        again, _ = optimize(POOL, Settings(lineups=4, min_unique=2),
                            projection_of=project)
        assert [
            sorted(p.source_player_id for p in lu) for lu in again
        ] == [sorted(p.source_player_id for p in lu) for lu in first]


def test_the_report_records_timing_per_lineup():
    _, report = optimize(POOL, Settings(lineups=3), projection_of=project)
    assert len(report.per_lineup_seconds) == 3
    assert report.seconds >= sum(report.per_lineup_seconds) * 0.9


@pytest.mark.slow
def test_the_report_names_its_formulation():
    _, sequential = optimize(POOL, Settings(lineups=1), projection_of=project)
    _, simultaneous = optimize_simultaneous(
        POOL, Settings(lineups=1, time_limit=30), projection_of=project
    )
    assert sequential.mode == "sequential"
    assert simultaneous.mode == "simultaneous"
    assert "sequential" in sequential.render()


# ---------------------------------------------------------------------------
# Remaining branches
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_the_simultaneous_formulation_reports_its_own_shortfall():
    """A time limit that bites must be reported, not silently truncated."""
    tiny = make_pool(per_position=1, games=2)
    lineups, report = optimize_simultaneous(
        tiny, Settings(lineups=30, min_unique=9, time_limit=5),
        projection_of=project,
    )
    if report.short:
        assert report.binding_constraint
        assert "WARNING" in report.render()


def test_exposure_relaxation_is_diagnosed():
    """A cap so tight nothing else can be built should name max_exposure."""
    tiny = make_pool(per_position=1, games=2)
    _, report = optimize(
        tiny, Settings(lineups=8, max_exposure=0.15, min_unique=1),
        projection_of=project,
    )
    if report.short:
        assert report.binding_constraint


def test_a_pool_too_small_to_field_a_lineup_produces_nothing():
    thin = [p for p in make_pool(per_position=1, games=2) if p.position == "QB"]
    lineups, report = optimize(thin, Settings(lineups=1), projection_of=project)
    assert lineups == []
    assert report.produced == 0
    assert report.binding_constraint


def test_a_default_projection_of_zero_still_builds_a_legal_lineup():
    """Callers without projections should still get a valid roster."""
    lineups, report = optimize(POOL, Settings(lineups=1))
    assert report.produced == 1
    assert_legal(lineups[0])


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------

def test_a_locked_player_appears_in_every_lineup():
    target = POOL[0]
    lineups, report = optimize(
        POOL, Settings(lineups=5, min_unique=1, locks=(target.source_player_id,)),
        projection_of=project,
    )
    assert report.produced == 5
    for lineup in lineups:
        assert target.source_player_id in {p.source_player_id for p in lineup}
        assert_legal(lineup)


def test_several_locks_are_all_honoured():
    from dfs_pipeline.optimizer import lock_shortfalls

    qb = next(p for p in POOL if p.position == "QB")
    dst = next(p for p in POOL if p.position == "DST")
    locks = (qb.source_player_id, dst.source_player_id)
    assert lock_shortfalls(POOL, locks) is None

    lineups, _ = optimize(POOL, Settings(lineups=3, locks=locks),
                          projection_of=project)
    for lineup in lineups:
        ids = {p.source_player_id for p in lineup}
        assert set(locks) <= ids
        assert_legal(lineup)


def test_locking_a_player_outside_the_pool_is_refused():
    """Usually means they were filtered out by status, which is worth saying."""
    from dfs_pipeline.optimizer import lock_shortfalls

    message = lock_shortfalls(POOL, ("no-such-id",))
    assert "not in the pool" in message
    assert "injury status" in message


def test_locking_two_quarterbacks_is_refused_before_solving():
    from dfs_pipeline.optimizer import lock_shortfalls

    qbs = tuple(p.source_player_id for p in POOL if p.position == "QB")[:2]
    message = lock_shortfalls(POOL, qbs)
    assert "2 locked QBs" in message
    assert "at most 1" in message


def test_more_locks_than_roster_slots_is_refused():
    from dfs_pipeline.optimizer import lock_shortfalls

    locks = tuple(p.source_player_id for p in POOL[:ROSTER_SIZE + 1])
    assert "roster slots" in lock_shortfalls(POOL, locks)


def test_an_impossible_lock_produces_no_lineups_and_says_why():
    qbs = tuple(p.source_player_id for p in POOL if p.position == "QB")[:2]
    lineups, report = optimize(POOL, Settings(lineups=1, locks=qbs),
                               projection_of=project)
    assert lineups == []
    assert "locked QBs" in report.binding_constraint


def test_no_locks_is_the_default():
    assert Settings().locks == ()
    assert optimize(POOL, Settings(lineups=1), projection_of=project)[1].produced == 1
