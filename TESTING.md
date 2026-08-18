# Testing

**705 tests, 98% coverage, ~42 seconds.** No test touches the network.

```bash
uv run pytest                                    # everything
uv run pytest -m "not slow"                      # fast loop, skips solver-heavy tests
uv run pytest --cov=dfs_pipeline --cov-report=term-missing
uv run pytest tests/test_scoring.py -v           # one file
```

---

## What the suite is actually for

This project's stated worst failure is not a crash. It is **silent
degradation** — a pipeline that reports success while quietly producing wrong
numbers. The prototype did exactly that: when a projection name failed to match
a salary name, it substituted a season average and said nothing.

So most of these tests are not asking "does this work?" They are asking
**"when this breaks, does anyone find out?"**

That shapes what gets tested. Nearly every module has more tests for its
failure paths than its happy path, and several tests assert that something
*refuses* rather than guesses.

---

## Five kinds of test, and why each exists

### 1. Verification against real files

Fixtures are **recorded from real sources**, not invented. Every one of them
broke a schema we had guessed:

| fixture | what it corrected |
|---|---|
| `dk_salaries_real_shape.csv` | a `Status` column the spec did not mention, with values `Q`/`IR`/`OUT` — never `DOUBTFUL` |
| `dk_draftables_sample.json` | a separate `draftableId` per roster slot: 44 rows for 27 players |
| `projections_dff_real.csv` | DFF's column is `ppg_projection`, and `game_date` is *not* a computation time |
| `fantasypros_*.csv` | repeated column names, and an FPTS column that is half-PPR |
| `dk_upload_template.csv` | a 500-lineup-per-file cap nobody had mentioned |
| `nflverse_*_2025wk1.json` | real stat lines behind DraftKings' own published averages |

A fixture written from imagination is rectangular and agreeable. Real files are
neither, and the difference is where the bugs were.

### 2. Cross-checks against an independent computation

Matching our own expectations proves nothing. These match somebody else's:

- **Roster constraints** proved equivalent to `pydfs-lineup-optimizer`'s
  independently written rules — two different formulations, same admissible
  lineups.
- **DK scoring** reproduces nflverse's own PPR totals for **355 of 355**
  players, differing only by the known DK-versus-standard deltas.
- **DK's published `AvgPointsPerGame`** is reproduced from raw stat lines to a
  median of 0.023 points — inside DraftKings' own rounding.
- **Slot assignment** cross-checked against `is_legal_roster_shape()`: one
  solves a matching problem, the other enumerates arithmetically, and they must
  never disagree.

### 3. Exhaustive proofs, where the space is small enough

- **Slot assignment**: all three legal roster shapes × every distinct player
  ordering — **32,760 assignments**, each validated slot by slot.
- **Scoring**: every component asserted individually *before* any composite,
  because a composite total can be right by cancellation.
- **Points-allowed tiers**: both edges of every band, where off-by-one lives.

### 4. Property-based tests

Hypothesis generates inputs nobody thought to write:

- name normalization is idempotent and never merges two real players
- any nine positions either assign validly or raise — never a partial result
- scoring is monotone in touchdowns; surrendering points never helps a defense
- the legality predicate and the shape enumeration agree on every input

### 5. Malformed-input fixtures

Every bad file fails with a message naming **file, row and column**:

```
dk_salaries_empty_salary.csv:2 [column 'Salary']: value is empty
```

Missing columns, non-numeric salaries, duplicate IDs, one-game slates,
unparseable game info, blank names, files that are directories. Never a
traceback, never silence.

---

## Guarantees the suite enforces

**Every lineup is DK-legal.** Roster shape, salary cap, distinct games, no
duplicate player, and it must survive slot assignment — checked structurally on
every lineup every formulation produces, and again after the solver returns.

**The append-only store cannot be edited.** Triggers reject `UPDATE` and
`DELETE` on observation history. Tests confirm the *database* refuses, not the
application.

**Bitemporal queries do not leak the future.** The central test records a
projection effective Saturday morning but captured Sunday, then asserts a
Saturday-23:00 query cannot see it. A companion test runs the naive
single-timestamp query and **asserts that it leaks** — pinning the bug in place
so nobody "simplifies" the correct query away.

**No code authenticates as the operator.** `test_no_authentication_code_exists`
reads the DraftKings adapter's own source and fails if `requests.post`,
`requests.Session`, `cookiejar`, `password`, `authorization` or `bearer` appear.
A convention nobody checks is a convention that erodes.

**No test reaches the network.** Live calls cost API credits, fail offline, and
change week to week as slates roll over. One guard test makes `requests.get`
raise and confirms a full capture still runs — added after four tests were
found hitting DraftKings for real.

---

## Conventions worth knowing

**Fixtures are guarded too.** Several tests assert the *fixture* still has the
property it exists to demonstrate — that the draftables sample still contains
duplicate roster-slot rows, that the FantasyPros header still repeats column
names. A fixture that quietly loses its teeth turns its tests green and
meaningless.

**Exhaustive tests assert their own coverage.** `assert checked > 10_000` in
the slot-assignment proof caught a generator bug that made the test run **zero
iterations** while passing.

**Slow tests are marked.** Solver-heavy tests carry `@pytest.mark.slow`, so
`pytest -m "not slow"` stays a fast loop while the full suite still exercises
both optimizer formulations.

**Deprecation warnings from pinned dependencies are filtered** in
`pyproject.toml`, with the reason stated inline, so they do not drown real ones.

---

## What the suite does *not* prove

Stated plainly, because a test suite that implies more than it checks is its own
kind of silent failure:

- **Rare scoring events** — safeties, two-point conversions, return
  touchdowns, offensive fumble-recovery touchdowns — are checked against
  DraftKings' *published rules* only. They are too rare to appear in the
  season-average cross-check, and closing this needs a real contest box score.
- **Realized ownership** is untested, because no contest export exists yet.
- **Nothing here predicts anything.** Every test is about correctness of
  computation and integrity of data. Whether the projections are any good is a
  question for the research roadmap, and no amount of green here speaks to it.
