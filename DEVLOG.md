# Development Log

A dated, append-only record of what was built, what broke, and what was
decided. Entries are written as they happen, including the wrong turns.

---

## 2026-08-17 — Session 1: assessment, environment, repository setup

### What this session did

Read-only assessment of the two prototypes and the two reference
repositories, environment setup, and repository initialization. **No changes
were made to the prototype scripts.** They are committed here exactly as they
were written, so that later hardening is visible as a diff rather than
asserted.

### Reference repositories inspected

| Repository | Commit | Commit date |
|---|---|---|
| `DimaKudosh/pydfs-lineup-optimizer` | `429db96891e91c326a14330c5fc29625ba6d11e8` | 2021-09-27 (tag 3.6.1) |
| `chanzer0/NFL-DFS-Tools` | `42e896f` | 2025-09-03 |

Hashes are recorded because both repositories will change and both assessments
should be reproducible.

### Finding 1 — our constraint logic is correct (VERIFIED)

`pydfs-lineup-optimizer` models DraftKings NFL Classic declaratively, as an
ordered list of roster slots:

```python
budget = 50000
min_games = 2
positions = [QB, RB, RB, WR, WR, WR, TE, FLEX(WR/RB/TE), DST]
```

Its `get_positions_for_optimizer()` converts that slot list into
minimum-count constraints:

```
QB >= 1,  RB >= 2,  WR >= 3,  TE >= 1,  DST >= 1,  {WR,RB,TE} >= 7,  total == 9
```

Ours (`dk_optimizer.py`) uses two-sided bounds instead:

```
QB == 1,  DST == 1,  2 <= RB <= 3,  3 <= WR <= 4,  1 <= TE <= 2,  total == 9
```

**Proof of equivalence.** Under the pydfs set, suppose QB = 2. Then
`RB + WR + TE <= 9 - 2 - 1 = 6`, contradicting `{WR,RB,TE} >= 7`. So QB = 1,
and the identical argument forces DST = 1. Therefore `RB + WR + TE == 7`
exactly. The floors 2 + 3 + 1 = 6 leave exactly one floating slot, yielding
RB in [2,3], WR in [3,4], TE in [1,2]. The two feasible sets are identical.

This also independently confirms three parameters previously held as belief
rather than fact: the **$50,000 cap**, the **minimum of two games**, and
**FLEX admitting RB/WR/TE only**.

**Caveat on authority.** The pydfs HEAD is from September 2021 and reflects
DK's rules as of that date. Agreement is reassuring for parameters that have
not changed, but DraftKings' published rules remain the source of truth. The
scoring constants in particular must be verified against DK directly.

### Finding 2 — sequential multi-lineup solving is the standard approach

`pydfs-lineup-optimizer`'s `optimize()` is a loop that copies the solver and
re-solves once per lineup:

```python
for _ in range(n):
    solver = base_solver.copy()
    ...
    solved_variables = solver.solve()
```

A mature, multi-sport library with six years of development made the same
architectural choice as our prototype. This does not prove a simultaneous
multi-lineup formulation is intractable, and the planned benchmark still runs
— but the framing changes. Sequential solving is the industry-standard
approach, to be documented with runtime evidence, not a shortcut requiring
apology.

### Finding 3 — chanzer0 consumes ownership, it does not model it

This reorders the research roadmap's dependencies, so it is the most
consequential finding of the session.

`NFL-DFS-Tools` builds a synthetic contest field by sampling lineups with
probability proportional to ownership:

```python
probabilities = ownership[valid_indices]
probabilities /= probabilities.sum()
chosen_index = rng.choice(valid_indices, p=probabilities)
```

Ownership arrives as an `own%` column in the user's own projections CSV. The
repository contains **no ownership model at all**. Every downstream number —
field composition, leverage, simulated ROI — is a function of an ownership
projection the user must source elsewhere.

**Consequence.** Field simulation is not an alternative to ownership
projection; it is strictly downstream of it. Building a simulator before we
can project ownership would produce confident output resting on an input we do
not have. Harvesting realized ownership from contest-results exports is
therefore a **prerequisite**, not a later refinement — and realized ownership
is exactly the point-in-time data that cannot be reconstructed after the fact.
This strengthens the Phase 0 deadline argument.

### Finding 4 — three defects worth learning from in chanzer0's simulation

The correlated-outcome machinery uses Iman–Conover rank reordering: draw
correlated standard normals via Cholesky, then permute each player's
independently-drawn marginal samples to match those ranks. This imposes rank
correlation while preserving arbitrary marginals exactly, which is the right
technique for a distribution as right-skewed as fantasy scoring. Three
criticisms, recorded because they are instructive for our own requirements:

1. **The post-hoc clamp undoes the moment matching.** The code trims to
   `Fpts + 5*StdDev`, affine-rescales to hit the target mean and standard
   deviation, and *then* applies `np.maximum(samples, 0)`. That final clamp
   reintroduces exactly the bias the rescale removed, and it bites hardest on
   low-projection, high-variance players — the punt plays that decide
   tournaments. The operations are in the wrong order.
2. **The affine rescale partly defeats the gamma marginals.** Fitting a
   non-negative, right-skewed distribution and then shifting it linearly can
   push mass below zero, which is then clamped away.
3. **Reproducibility hole.** The Cholesky step draws from the global NumPy RNG
   rather than the seeded generator used elsewhere, so runs are not
   reproducible from a seed. Noted precisely because our own acceptance
   criteria require seed-reproducible runs.

Credit where due: `calculate_payouts()` correctly handles tied lineups by
splitting the summed prize across the tied block, which is how DraftKings
actually pays ties and which naive implementations get wrong.

### Defects confirmed in our own prototypes

Constraint logic: **no bugs found.** Non-constraint defects carried into
Phase 1:

| # | Location | Issue |
|---|---|---|
| 1 | `dk_optimizer.py:278` | `assign_slots()` writes an empty cell on failure instead of raising. pydfs raises `LineupOptimizerException`. The greedy pass happens to be correct for all three legal DK multisets, but by luck of the constraint structure, not by construction. |
| 2 | `dk_optimizer.py:243` | Infeasibility does not name the binding constraint. pydfs reports which user-defined constraint failed. |
| 3 | `dk_optimizer.py:165` | Only one exposure strategy, with a truncating cap. pydfs also offers `AfterEachExposureStrategy`, which enforces the ratio at each iteration rather than against the total — distributing exposure evenly instead of spending the best players in the earliest lineups. |
| 4 | `dk_vegas_adjust.py:44` | Docstring worked example states "48.5 total with KC at -2.5 gives KC 25.75" — the code's own formula gives 25.5. The documented numbers correspond to a -3.0 spread. Code is correct; documentation is not. |
| 5 | `dk_vegas_adjust.py:168` | A blank `Position` field routes a DST into the offense branch, scaling it by its *own* implied total rather than inversely by the opponent's. Fails silently and produces plausible-looking output. |
| 6 | `dk_optimizer.py:149` vs `:202` | Bans match case-sensitively, locks case-insensitively. A lock on a duplicated name forces "exactly one player of that name," not the intended player. |
| 7 | both scripts | Name-keyed joins end to end, degrading silently to `AvgPointsPerGame`. Already the top Phase 1 priority. |

### Environment

- **Blocker resolved.** The machine had only Apple's system Python 3.9.6.
  `nflreadpy` requires `>=3.10` (verified against PyPI: v0.1.5). Installed
  `uv` via Homebrew and Python **3.12.14** through it, isolated from the
  system interpreter. Never install packages into the OS Python; macOS owns
  it and an update can break the environment.
- Git identity configured with a GitHub noreply address, so a personal email
  is not embedded permanently in a public commit history.

### Decisions

| Decision | Rationale |
|---|---|
| Scripts committed unmodified as the initial commit | The commit history is a portfolio deliverable. Hardening should be visible as a diff, not asserted after the fact. |
| `uv` over `venv` + `pip` | One tool for Python versions, environments, and dependencies; substantially faster; does not touch the system interpreter. |
| Publish `RESEARCH_ROADMAP.md`; keep the source handoff and case-study template local | The handoff is an *input* to the work, written in first person to an assistant. Its substantive content — domain rules, acceptance criteria, non-goals — is restated in README/TESTING/DEVLOG as project documentation, which is where a reviewer expects it. Nothing substantive is withheld; what stays local is pedagogy, not project. |
| GitHub noreply commit email | A public repository records the author email permanently, and bots scrape it. |

### Package skeleton (same session)

Stood up `pyproject.toml`, a `src/` layout, and pytest. 25 tests passing
against Python 3.12.14.

**Why `src/` rather than a top-level package.** With a top-level layout,
Python resolves imports from the current working directory, so a test suite
can pass against code that was never actually installed — packaging breaks
silently and only surfaces when someone else clones the repo. A `src/` layout
makes that impossible: tests can only reach the package through the
installed environment.

**Dependency pinning split.** `pyproject.toml` declares compatible ranges;
`uv.lock` records exact resolved versions and is committed. The ranges say
what the code tolerates, the lockfile says what was actually tested.

**`dfs-snapshot` exists and exits non-zero.** The console script is wired up
and on PATH, but no capture logic is written, so it prints its status to
stderr and returns exit code 2. A stub that printed something friendly and
returned 0 would be reporting success it had not earned — the exact failure
class this project treats as worse than crashing. A test pins that behaviour
so it cannot quietly become a no-op success.

**First domain module: `dfs_pipeline.contest`.** Contest rules as data in one
auditable place. Includes `legal_roster_shapes()`, which *derives* the three
legal position-count combinations from the bounds rather than hardcoding
them, so the enumeration cannot drift out of sync with the constraints it
expresses. A Hypothesis property test cross-checks the arithmetic predicate
against the brute-force enumeration over thousands of generated inputs — two
independent implementations of the same rule, required never to disagree.

Scoring constants are deliberately **not** in this module yet. They require
verification against DraftKings' published rules plus a fixture pairing real
stat lines with published point totals. Encoding them now would look like
knowledge we do not have.

### Two test failures on first run, both instructive

1. `TypeError: unhashable type: 'dict'` — a genuinely bad line of test code
   (`{dict(s) for s in ...}` builds a set of dicts). Fixed by writing what
   was meant: `len(legal_roster_shapes()) == 3`.

2. A test asserted the package would import from site-packages rather than
   the source tree. It failed, and **the test was wrong, not the code**.
   `uv sync` installs the project in editable mode, which correctly points
   the environment back at `src/`. The property actually worth testing is
   that the package imports from an *unrelated working directory* — that can
   only succeed if the environment genuinely knows about the package.
   Rewritten accordingly.

Recording the second one because the failure mode is subtle: the test was
green-adjacent nonsense that would have "passed" under a worse packaging
setup and failed under a correct one. A test that asserts the wrong invariant
is worse than no test, because it manufactures confidence.

### Storage decision: SQLite, with raw artifacts kept as files beside it

**Rejected the usual framing.** Parquet is normally chosen over SQLite for
scale. Estimated volume is ~6,600 rows/week — under 15 MB per season, roughly
600k rows across five seasons. Columnar compression and scan throughput solve
problems three orders of magnitude larger than ours, so performance cannot
decide this. Any argument resting on it would be cargo-cult.

What decided it, in weight order:

1. **Integrity enforced by the engine, not by convention.** Append-only
   history is the foundation of every point-in-time claim this project makes.
   Parquet has no constraints: nothing stops a re-run writing a duplicate
   observation. In SQLite the UNIQUE constraint, the CHECK constraints and
   the append-only triggers hold even against someone poking at the database
   from a SQLite browser in Week 12.
2. **The as-of query is inherently relational** — partition by subject,
   order by two timestamps, take the top row, join across sources. Twelve
   lines of SQL that can be read and verified by eye. This is the one query
   the entire backtest layer depends on being correct.
3. **Maintainability.** The stated goal is maintaining this without
   assistance by season's end. SQLite means opening the file in any browser
   and writing SQL; Parquet means writing Python.
4. **The crosswalk is mutable.** Persistent identity resolutions with
   `first_seen` / `last_seen` / `review_status` are point lookups and updates
   — a database table, not an immutable file dump.

**DuckDB considered and deferred.** Technically the strongest contender: SQL
semantics plus native Parquet. Rejected for now because it adds a dependency
to solve a scale problem we do not have, and its constraint support is less
complete than SQLite's. If the store ever outgrows SQLite, DuckDB reads the
same data — a reason to defer the decision rather than make it now.

**Honest cost of the choice: schema migrations.** A source adding a column
means an `ALTER TABLE` where Parquet would just absorb it. Mitigated by the
narrow-table design below, which turns most upstream changes into new rows
rather than new columns.

### Raw artifacts are the part that matters more than the format

Raw bytes are written to `raw/<first-two-hex>/<sha256>.bin` and never
modified; the digest is recorded against every observation parsed from them.

The reasoning: **if a parser has a bug, normalized-only storage bakes that bug
in permanently.** Discover in December that September's odds timestamps were
mis-parsed, and with raw artifacts you re-parse and recover. Without them,
September is corrupt forever — and September cannot be re-downloaded at any
price. It also supplies the literal proof required: "this is exactly the file
we used on September 13," verifiable by hash. `artifact_bytes()` re-verifies
the digest on every read, so silent bit-rot in the raw zone cannot pass.

### Schema notes

**Narrow observations, controlled vocabulary.** Observations are stored as
`(subject, metric, value)` rather than one column per statistic, so a new
statistic is a new row rather than a migration, and the as-of query is written
and tested once instead of once per table. The usual cost of that flexibility
is losing a controlled vocabulary — typo `projeciton` and you have silently
created a new metric. The `metric` table closes that: metric names are foreign
keys, so an unregistered name is rejected, while adding a legitimate metric
stays a data insert.

**Capture never blocks on identity resolution.** Observations key on
`(source, source_subject_id)` — whatever identifier the source itself used.
Resolution onto nflverse IDs happens later via `crosswalk`. Dropping an
observation because a name did not match would be the worst possible trade,
since the data cannot be re-obtained.

**Clock-skew tolerance.** A CHECK constraint enforces
`effective_at <= captured_at`, which catches the classic bug of passing the
two timestamps in the wrong order. It allows 60 seconds of slack, because
source clocks are not synchronised with ours and a source stamping an event
a few seconds ahead of our wall clock is skew, not a data error. Without the
tolerance this would have failed spuriously in production.

**Foreign keys had to be switched on explicitly.** SQLite ships with
`PRAGMA foreign_keys` defaulting to OFF. Every `REFERENCES` clause in a schema
is decorative until a connection turns it on — a silent-integrity trap
directly counter to this project's premise. There is now a test asserting the
pragma is enabled, because the failure mode is invisible.

### Three failures worth recording

1. **`executescript()` issues an implicit COMMIT before running.** Wrapping
   the DDL in an explicit transaction produced `cannot commit - no
   transaction is active` and took out 25 tests at once. Documented Python
   behaviour, but surprising. DDL now runs outside the transaction; only
   seed data is transactional.

2. **A weak assertion that proved nothing.** The central Sunday-capture test
   originally ended with `assert 21.5 in sunday or sunday == {17.1}` — an
   assertion accepting two contradictory outcomes. It passed, and it tested
   nothing. Replaced with exact assertions on value, effective_at and total
   observation count. This is the second time in one session a test asserted
   the wrong invariant; both are recorded because a test that manufactures
   confidence is worse than no test.

3. **Coverage refused to combine subprocess data.** The packaging tests spawn
   children, one with `cwd=tmp_path`, where coverage cannot find
   `pyproject.toml`, silently falls back to statement-only mode, and then
   fails to merge with the parent's branch data. Those children are testing
   packaging, not coverage, so they now run with the instrumentation
   environment stripped.

Store implementation: 61 tests, **100% line and branch coverage** on
`dfs_pipeline`.

### Open items

- Verify DK Classic scoring constants against DraftKings' published rules
  (**BLOCKED** — needs the current published rules page).
- Verify the bulk-upload CSV format against a real DK entries template
  (**BLOCKED** — needs slates to open).
- Benchmark simultaneous vs sequential multi-lineup solving at realistic scale
  (~500 players, 20–150 lineups).
- Select a license.
