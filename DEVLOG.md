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

### DKSalaries.csv import path, and schema VERIFIED against a real export

Built the manual CSV import first, ahead of the unofficial API path. It is the
fallback the whole design depends on: when the draftables endpoint breaks —
plausibly mid-slate, since it carries no stability guarantee — the manual
export must still work. Building it first also means the eventual golden-file
equivalence test has something to compare against.

**Schema status moved UNVERIFIED → VERIFIED (2026-08-17)** against a real
DraftKings export for the 2026 Week 1 main slate. It parsed on the first
attempt: 716 entries (692 players + 24 defenses), 12 games, 24 teams, every
kickoff converted correctly to UTC. 5,117 observations ingested in 209 ms;
the as-of query returns all 716 salaries in 5.2 ms.

Confirmed present in the real file and already handled: a **UTF-8 BOM**
(Excel's doing), **CRLF line endings**, `Roster Position` values of the form
`RB/FLEX`, and `AvgPointsPerGame` sometimes written as a bare integer.

### The real file corrected an assumption in the handoff

The export carries a **`Status` column** that was not in the assumed layout,
and its vocabulary is not what the specification predicted:

| Status | Count |
|---|---|
| *(empty)* | 611 |
| `Q` | 82 |
| `IR` | 15 |
| `OUT` | 8 |

The handoff specifies `--exclude-status OUT,DOUBTFUL`. **DraftKings does not
emit `DOUBTFUL`.** A filter written to that spec would have matched nothing
for doubtful players and left 15 IR players in the pool at full salary — a
silent wrongness that produces plausible lineups built on unplayable players.
Found three weeks before Week 1 rather than during it, which is the entire
argument for testing against real data early.

Status is now captured as a `dk_status` observation but **never acted on in
the adapter**. Whether to exclude a designation is the optimizer's decision,
and a player's status *at capture time* is itself point-in-time data that
cannot be reconstructed later. Filtering at ingest would discard it
permanently.

The real file also confirmed the identity-resolution hazards the handoff
anticipated: apostrophes (`Ja'Marr Chase`, `De'Von Achane`), suffixes
(`Travis Etienne Jr.`, `Aaron Jones Sr.`), initials (`C.J. Stroud`), and
hyphenation (`Jacory Croskey-Merritt`). Defenses are named by nickname
(`Chargers`) with the code in `TeamAbbrev` (`LAC`), so the 32-team alias map
is genuinely required.

### Correction to the storage-decision arithmetic

The SQLite-vs-Parquet argument cited ~6,600 rows/week. A single slate capture
alone produces **5,117**, so with repeat captures plus projections, odds and
results the real figure is nearer 15–20k/week — roughly 3× the estimate.
Recorded because that number was offered as decision evidence. The conclusion
is unchanged: ~300k rows/season is still orders of magnitude below where
columnar storage earns its complexity.

### Design notes

**Validation is strict about breakage, tolerant of additions.** The adapter
requires a subset of columns rather than an exact header match, because
DraftKings adds columns over time without removing existing ones — `Status`
being exactly that case. An exact-match check would have failed on a harmless
change; a subset check caught nothing spurious and would still catch a real
removal.

**Slate-level invariants are checked, not just row-level ones.** Duplicate
player IDs are rejected (they would silently double a player's exposure
downstream), and a single-game file is rejected with a message naming it as a
probable Showdown slate rather than failing later as an infeasible solve.

**Archive before parsing.** `ingest_slate` stores the raw bytes *first*, so a
parse failure still leaves the artifact on disk. A week's slate cannot be
re-downloaded; an unparsed artifact is recoverable, an unarchived one is not.
There is a test asserting the artifact survives a deliberate parse failure.

**A fixture derived from the real file** (`dk_salaries_real_shape.csv`, 27
rows) now carries the BOM, CRLF, Status values, and awkward names, with a test
guarding the fixture's own structural features so it cannot quietly stop
testing them. The hand-written fixtures test what we assumed; this one tests
what DraftKings actually emits.

The real 716-row export itself lives in the gitignored `_local/real_slates/`
and is not committed.

110 tests, **100% line and branch coverage**.

### `dfs-snapshot` wired to the CSV path — the pipeline is now runnable

Three working components became one command:

```
dfs-snapshot --salaries DKSalaries.csv
```

Against the real 716-row export: 5,117 observations captured, artifact
archived and hashed, run directory written. If the season started tomorrow,
a slate could be captured today.

**Configuration precedence is one-directional and recorded.** Built-in
defaults < `dfs.toml` < command-line flags. A weekly command should not
require re-typing six paths, but a config file must never silently win over
something the operator typed. Every resolved value keeps its origin, so
`--show-config` answers "why is it using *that* store?" without reading
source.

**A malformed config is a hard failure, not a fallback.** Silently ignoring a
config the operator wrote — and running against a different store than they
intended — is the same class of quiet wrongness as a silent name-match
failure. Unknown sections and unknown keys are rejected too, so a typo'd
`[stoer]` is caught rather than ignored.

**Console quiet, log verbose.** Default console output is warnings only; the
per-run `run.log` always records everything at DEBUG. Otherwise diagnosing a
Week 7 failure depends on having thought to pass `-v` in Week 7.

**Exit codes distinguish kinds of failure**, because a caller scripting this
needs to tell "fix your CSV" from "fix your machine":

| Code | Meaning |
|---|---|
| 0 | capture succeeded |
| 1 | runtime failure (store unavailable, permissions) |
| 2 | usage error |
| 3 | input data rejected — schema or validation failure |

**The run directory is written even when the run fails**, with the error
recorded. A run that vanishes when it breaks cannot be debugged on a Sunday
morning. `run.json` carries the resolved config with origins, the SHA-256 and
byte size of every input, package/Python/platform versions, timestamps, and
the outcome.

**Randomness is recorded as `"none"` rather than omitted.** There is no seed
because nothing in the capture path is nondeterministic. Stating that
explicitly lets a reader *confirm* determinism instead of assuming it. When
the optimizer lands — solver tie-breaking is the first real source of
nondeterminism — this becomes a recorded seed. Adding a `--seed` flag now
would be a placebo.

### Two bugs found by writing the tests

**1. Run directories collided and silently destroyed history.** Run ids are
timestamped to the second, and four rapid runs produced *one* directory —
each overwriting the last one's metadata. Re-running a failed capture
immediately is completely ordinary, so this was a real collision, not a
theoretical one, and it destroyed exactly the audit trail the class exists to
provide. Fixed with `mkdir(exist_ok=False)` and a numeric suffix, which is
race-free in a way that checking-then-creating is not. Regression test pins
it.

**2. `sqlite3.OperationalError` escaped as a raw traceback.** The CLI caught
`StoreError` and `OSError`, but sqlite3's exceptions descend from neither, so
an unopenable database file produced a stack trace instead of a message. The
acceptance criterion is "never a traceback, never silence" — and that applies
to the environment failing just as much as to bad input. Caught explicitly
now, with the reason commented at the except clause.

One test of mine was also simply wrong: it created a *directory* named
`dfs.toml` expecting a read error, but `is_file()` correctly skips that.
Rewritten to use an unreadable file, which is the real case.

146 tests, 100% statement coverage, 99% branch. Two partial branches remain
(an error-message conditional and a loop-exit path); left uncovered rather
than padded with artificial tests.

### DK Classic scoring encoded — the last BLOCKED item, cleared

DraftKings' published NFL Classic rules were supplied, so the scoring
constants moved from BLOCKED to **VERIFIED against the published rules
(2026-08-17)**. All constants live in one module, `dfs_pipeline.scoring`, and
the test suite asserts each component independently before checking any
composite. That ordering matters: a composite total can be correct by
cancellation — two errors of equal magnitude and opposite sign produce a
passing test and a broken scorer.

**Verification is deliberately split into two claims**, because collapsing
them would overstate what we know:

- **VERIFIED:** the implementation matches DraftKings' published rules,
  component by component.
- **UNVERIFIED:** that it reproduces a *DraftKings-published player total*
  for a real game. That needs a real contest box score to compare against.
  Matching the stated rules and matching DK's own output are different
  claims, and the acceptance criterion asks for the second.

### The continuous-yardage trap

DraftKings words the yardage rules as "+1 Pt per 25 Passing Yards
(+0.04 Pts/Yard)". The parenthetical governs — scoring is **continuous, not
stepped**:

| Passing yards | Correct | Integer division | Error |
|---|---|---|---|
| 287 | 11.48 | 11.00 | **+0.48** |
| 299 | 11.96 | 11.00 | **+0.96** |
| 349 | 16.96 | 16.00 | **+0.96** |

An implementation using `yards // 25` under-scores nearly every player on
every slate by up to a point — small, plausible, uniformly wrong, and
invisible without a component-level test. Errors of that size are decisive in
a contest where lineups separate by fractions. The same applies to the 0.1/yard
rushing and receiving rules, and negative yardage scores negatively (a
quarterback with -3 rushing yards loses 0.3).

### Other details the published rules settled

- **Bonus thresholds are inclusive.** "300+" and "100+" mean exactly 300 and
  exactly 100 qualify. Tested at both edges.
- **Rushing and receiving 100-yard bonuses stack** for a player who crosses
  both.
- **Points allowed is DST-attributable only** — points the team's own offense
  surrenders (a pick-six thrown by your quarterback) are not charged to the
  defense. The field is documented as requiring the attributable figure, not
  the opponent's final score. This is a real modelling hazard when results
  ingestion lands, because the naive source for "points allowed" is the box
  score's final total, which is wrong.
- **Half-sacks score 0.5**, so the sacks field is a float.
- **Points-allowed tiers tested at every boundary** (0/1/6/7/13/14/20/21/27/
  28/34/35), where off-by-one errors live. A property test also asserts the
  tier function is monotone — surrendering more points can never help.
- Negative points-allowed raises rather than scoring as the shutout tier,
  which would quietly reward a data bug.

### A test failure worth recording

Two scoring tests failed on first run, and **the code was right — my test
expectations were wrong.** I asserted 137 rushing yards scores 13.7, having
forgotten the 100-yard bonus I had just implemented; the correct answer is
16.7. Fixed by moving the continuity cases below 100 so they isolate what
they claim to test, and adding a separate case for yardage-plus-bonus.

This is the third time in the project that a test asserted the wrong
invariant. The pattern is consistent enough to name: writing the test from
the same mental model that wrote the code reproduces the model's blind spots.
The defence that keeps working is deriving expected values from the *source
document* arithmetic rather than from the implementation — which is why every
composite test now spells out its terms in the docstring.

217 tests, 100% statement coverage.

### Odds ingest — and the identity layer it forced

Live capture works: `dfs-snapshot --salaries ... --odds` records a slate and a
betting snapshot in one invocation. Against real data: 716 slate entries plus
16 games x 9 bookmakers, **12 of 12 slate games joined to odds** by game key.

**Quota is a design constraint, not a footnote.** The free tier is 500
requests/month and **cost is one credit per region per market, not per call** —
`us` with `spreads,totals` costs 2. That multiplier is how a month disappears
in an afternoon. So: the cost is computed and logged before the call, remaining
credits are read from response headers and logged every time, and the adapter
**refuses to run below a floor** (`--min-quota`, default 25) so a scheduled job
cannot exhaust the budget and leave a live slate uncaptured. Quota can be
checked for free — `/v4/sports` returns the same headers at zero cost — which
is what `--quota` uses.

**The API returns the whole season without a window.** The first exploratory
call came back with **272 events**, nearly all irrelevant to any one slate.
`commenceTimeFrom`/`commenceTimeTo` now bound it (`--odds-days`, default 8).
Worth noting the window does not reduce cost — cost is per region per market
regardless — it reduces noise.

**Bitemporality finally earns its keep.** Unlike the DraftKings CSV, which
carries no timestamp of its own, every odds market reports its own
`last_update`. That is a genuine `effective_at` distinct from `captured_at`:
a line that moved at 16:40 and was read by us at 17:05. This is the case the
store was built for, and it arrives per-row rather than per-capture — one book
may have moved minutes ago and another hours ago.

**Every bookmaker is kept, not reduced to a consensus.** Each becomes its own
source (`ODDS_API:draftkings`). Books disagree, and that disagreement is
signal which cannot be recovered from a consensus computed at capture time and
stored alone. Consensus is a modelling decision, and modelling decisions belong
downstream of capture.

**Derived values are computed, not stored... except one.** `implied_team_total`
is `(total / 2) - (spread / 2)`. It is a property on the record rather than a
stored field, so it cannot drift from its inputs — but it *is* also written as
an observation, because a backtest asking "what was the implied total at
Saturday 23:00" should not have to re-derive it from two separately-resolved
rows. The property is the definition; the stored value is a convenience that
the property generates.

### The team identity layer

The Odds API says `Seattle Seahawks`; DraftKings says `SEA` and names defenses
`Seahawks`. `dfs_pipeline.teams` resolves all three, and this also satisfies
the handoff's 32-team DST alias requirement — a defense is a team-level
entity, not a player with an odd name, and resolving `Chargers` to `LAC` must
never run through logic designed for human names.

**No fuzzy matching, deliberately.** There are exactly 32 teams and they change
about once a decade, so the mapping can be exhaustive. With 32 possible
answers a fuzzy match that fires is a bug, not a rescue. Unknown teams raise;
a silently unresolved team would drop half a game's odds while the pipeline
looked healthy.

Historical codes (`OAK`, `SD`, `STL`, `WFT`) resolve to current franchises, so
older nflverse seasons need no special-casing at the call site. Alias
collisions raise at import — an alias quietly shadowing another team's key
would misroute an entire franchise's data with no visible symptom.

Validated against **both** live vocabularies: every abbreviation and every DST
nickname from the real DraftKings export, and all 32 full names from the real
Odds API response.

### What the recorded fixture caught that an invented one would not

Four tests failed asserting 54 spread rows across 3 games x 2 teams x 9 books.
The parser was right: **`mybookieag` posted a total for SF@LAR but no spread.**
Real books are not uniform. 52 rows have spreads, 54 have totals, and the two
incomplete rows correctly carry `implied_team_total = None` rather than a value
derived from a guess.

A fixture written from imagination would have been perfectly rectangular and
would have hidden this. It is the same lesson the real `DKSalaries.csv` taught
when it revealed the `Status` column — recorded reality beats invented
reality, and cheaply.

One of my own tests was also wrong again: I listed `K.C` as an unresolvable
string, but the normalizer strips punctuation *on purpose* so that `K.C.` and
`A.J.` resolve. Replaced with a test asserting that behaviour rather than
forbidding it.

### Secrets handling

`dfs_pipeline.secrets` reads credentials from the environment first, then
`.env`. It deliberately **does not mutate `os.environ`** — a secret injected
there leaks into every subprocess and crash report thereafter. The API key is
scrubbed from every error message and log line the adapter produces, with
tests asserting it, because credentials escape through exception text far more
often than through source code. The `.env.example` placeholder counts as
missing, so an unfilled copy produces "ODDS_API_KEY is not set" rather than a
confusing 401.

313 tests, 100% statement coverage.

### Results ingest — nflverse scored at DraftKings rules

`dfs-snapshot --results --season 2025 --week 1` scores a completed week: 359
players plus 32 defenses, 1,564 observations. This closes the loop from "what
we knew" to "what happened".

**Validated against an independent computation.** nflverse publishes its own
PPR total from the same underlying statistics. DraftKings differs in exactly
two ways — it charges −1 for interceptions and lost fumbles where standard
scoring charges −2, and it adds 300/100-yard bonuses — so this identity must
hold:

    dk = ppr + interceptions + fumbles_lost + bonuses

It held for **355 of 355** scored players in 2025 week 1. That is a real
cross-check: if the mapping picked up a wrong column, the identity breaks.
Matching my own expectations proves nothing; matching someone else's
independent arithmetic proves the column mapping.

### The points-allowed trap, made concrete

nflverse has **no points-allowed column at any level** — not in `player_stats`,
not in `team_stats`, nowhere. It has to be derived, and the obvious source is
wrong.

DraftKings charges a defense only for points surrendered while the DST was on
the field. A pick-six thrown by your own quarterback is not charged to your
defense. But **`td_team == defteam` is not the discriminator**. In 2025 weeks
1–4, 18 touchdowns were scored by the team on defense for that play:

| play_type | count | DK treatment |
|---|---|---|
| `pass` | 8 | excluded — scored against our offense |
| `punt` | 7 | **counts** — special teams |
| `field_goal` | 2 | **counts** — DK lists "FG Return TDs" |
| `kickoff` | 1 | **counts** — special teams |

A rule keyed on "the defense scored" would wrongly forgive 10 of 18. The
discriminator is `play_type`, and `SCRIMMAGE_PLAY_TYPES` is asserted by a test
precisely because widening it silently forgives return touchdowns.

Concretely: Chicago beat Minnesota 24–21 in week 1 with a pick-six. Minnesota's
defense is charged **18**, not 24 — which moves it from the 21–27 tier (0 pts)
to the 14–20 tier (+1). Small, but wrong every week and always in the same
direction. Exactly one team in week 1 differs from its opponent's final score,
and there is a test asserting that blast radius so a widened rule fails loudly.

**Documented ambiguity:** the extra point after an opponent's pick-six. DK
lists "Extra-points" as points allowed without qualification, so it is counted
even though the touchdown it follows is not. Recorded because it is a
judgement call, not a derivation.

### A bug the alias map was supposed to prevent — and caught

`defense_results` resolved team codes through the alias map; `points_allowed_by_team`
did not. **nflverse is not internally consistent**: play-by-play writes the
Rams as `LA`, `team_stats` writes `LAR`. So the lookup missed and an entire
defense was silently dropped from the week, with only a log line.

Applying an alias layer on one side only is worse than not having one, because
the failure is invisible — the pipeline reports success with 31 defenses
instead of 32. Fixed by resolving on both sides; regression test asserts `LAR`
is present and raw `LA` never leaks into the keys.

### Design notes

**Results key on nflverse ids, not DraftKings ids.** Results come from a
different source with its own identifiers. Forcing a join at capture time would
drop any player the crosswalk cannot yet resolve, and capture must never lose
data to an unresolved name. Joining is Phase 1's problem.

**The archived artifact is the input, not the output.** `load_and_score_week`
stores the source rows plus the derived points-allowed figures — not our
computed scores. Archiving only output would bake any scoring bug in
permanently, which is the opposite of why raw artifacts exist. A test asserts
`dk_points` does not appear in the artifact.

**Re-capture is new history, not an overwrite.** nflverse revises prior weeks
as official corrections land. A later capture records new observations with a
later `captured_at`, so a revision is visible rather than silently replacing
what we scored on at the time.

**A team without an attributable points-allowed figure is skipped, not
defaulted to zero** — defaulting would award a shutout the defense did not
earn.

348 tests, 99% coverage.

### Projections ingest — and the real DFF export corrected three assumptions

The last unrecoverable stream is now captured. A projection as it stood on
Saturday cannot be bought on Monday, so this closes the Phase 0 gap that
mattered most.

**A real Daily Fantasy Fuel export arrived mid-build and rejected my reader.**
That rejection was the design working. DFF's actual columns are
`ppg_projection` and `ownership_projection` — neither resembling the obvious
guess — and the error named every alias tried plus every header present, which
turned a potential investigation into a one-line fix. A lenient reader would
have guessed a column and produced plausible garbage; that is the failure this
project treats as worst.

Three corrections the real file forced:

1. **`ppg_projection` / `ownership_projection`** added as aliases. Schema moved
   UNVERIFIED → **VERIFIED 2026-08-17**.
2. **`value_projection` deliberately excluded.** It is points per $1,000 of
   salary, not projected points. Reading it would yield numbers around 3.0 —
   entirely plausible, entirely wrong. There is a test asserting it stays out.
3. **`game_date` must never become `effective_at`.** It names the day the games
   are played, not when the projection was computed, and it sits in the
   *future* relative to capture. Treating it as `effective_at` would assert we
   knew Sunday's numbers weeks early — and the store's clock-skew CHECK would
   have rejected the row outright. My own fixture had encoded exactly this
   wrong assumption (`slate_date` as a timestamp); the real file exposed it.
   Verified: a real DFF export states no computation time at all, so
   `effective_at` honestly defaults to `captured_at`.

The export itself was 9 rows, all `injury_status = O`, all projections 0.0, no
ownership — a filtered slice, not a projection set, consistent with the site
being glitchy. Capture does not care: recording what the vendor actually served
is the job, and the match report is where a thin file becomes visible.

### Three sources, three capture times, three answers

The most striking artefact of the day, from real data:

| Player | DK CSV 11:20 | DK API 12:00 | DFF 12:31 |
|---|---|---|---|
| **Chris Bell** | *(clear)* | **Q** | **O** |
| Alec Pierce | OUT | OUT | O |
| Tank Bigsby | *(clear)* | **Q** | — |

Chris Bell's designation escalated twice inside 70 minutes. Any single-timestamp
store would have silently kept whichever it saw last, and a backtest would have
believed we knew on Saturday morning what we only learned Saturday lunchtime.
This is the bitemporal argument, observed rather than asserted.

### Name normalization

Projections arrive keyed by name, which the handoff calls the biggest failure
risk. `dfs_pipeline.names` produces a deterministic key: strip accents,
casefold, delete periods and apostrophes (`C.J.` → `cj`, `Ja'Marr` → `jamarr`),
convert hyphens to spaces (`Croskey-Merritt` matches `Croskey Merritt`), drop
trailing generational suffixes (`Travis Etienne Jr.` → `travis etienne`).

**Validated on 692 real players: zero collisions.** That is the property that
matters — merging two people is far worse than failing to match one, because it
attaches one player's projection to another's salary, silently.

The module is explicitly *not* a cross-source matcher. It provides a stable key
*within* a source so next week's capture aligns with this week's history.
Resolving names onto nflverse ids needs team and position agreement, a
persistent crosswalk, and a human for the residue — that is Phase 1.

Where two rows share a name key and team/position cannot separate them, the
adapter **raises** rather than merging. Two players genuinely can share a name
on one slate.

### The match report

The prototype's worst defect was a silent name-match failure that degraded
projections invisibly. So when both a slate and projections are supplied, every
run prints:

```
  match rate   : 99.4% (688/692 slate players)
  WARNING: 4 slate player(s) at $5,000+ have no projection:
    $ 8,000  Jahmyr Gibbs (RB)
```

Unmatched players are reported *above a salary floor* — a missing $3,000 punt is
noise, a missing $8,000 player is a hole. Orphan projection names are listed
too. A match rate nobody reports is a match rate nobody checks.

Verified against the real 12-game slate with four expensive players deliberately
withheld: all four were named, both phantom rows were flagged.

### Design notes

**Each vendor is its own source.** DFF and FantasyPros never merge into a
consensus at capture time — vendors disagreeing is signal, and averaging
destroys it irreversibly. Consensus is a modelling decision, and modelling
belongs downstream of capture.

**FantasyPros remains BLOCKED** pending an API key.

Follow-up noted: the real DFF export also carries `spread`, `over_under` and
`implied_team_score`. That is direct evidence for the double-counting caveat in
`dk_vegas_adjust.py` — DFF projections already incorporate Vegas, so applying a
full-strength market adjustment on top of them counts it twice. Worth capturing
those columns later so the question is testable rather than argued.

421 tests, 99% coverage.

### FantasyPros as a second vendor — and three traps in one file format

Added `FantasyProsCsvAdapter`, verified against real per-position exports.
All five positions ingest: 633 rows, 2,532 observations.

**Trap 1: column names repeat, so parsing must be positional.**

```
QB header: Player,Team,ATT,CMP,YDS,TDS,INTS,ATT,YDS,TDS,FL,FPTS
                        ^^^          ^^^  ^^^
                        passing      rushing
```

`csv.DictReader` silently keeps the *last* duplicate. Ask it for Jalen Hurts'
`YDS` and it returns **27.3** — his rushing yards — while the passing value of
217.5 is discarded. Every layout is now declared by index and the header is
verified *exactly* before any row is read, so a changed header is a loud
failure rather than a silent misread of every field. A test reproduces the
DictReader bug directly, so the reason for the positional design cannot be
optimised away later.

**Trap 2: their FPTS column is half-PPR. DraftKings is full PPR.**

Verified against their own component stats — every running back's published
FPTS matches `base + 0.5 × receptions`:

| Position | our DK re-score − their FPTS |
|---|---|
| QB | +0.00 to +0.43 |
| RB | +1.8 to +2.7 |
| **WR** | **+3.3 to +3.7** |
| TE | +2.4 to +3.4 |

Ja'Marr Chase differs by **+3.66 points**. The pattern is exactly 0.5 ×
receptions, which is why quarterbacks barely move and receivers move a lot.
Taking that column directly would under-project every pass-catcher,
systematically, in a way that looks entirely reasonable.

So the FPTS column is **ignored**. Their projected stat lines are scored with
`dfs_pipeline.scoring` — the same canonical rules applied to realized nflverse
results. One definition of DraftKings scoring now governs projections, results
and the optimizer alike.

The half-PPR test is written as a *comparative* fit rather than an absolute
tolerance, because FantasyPros rounds each component to one decimal while
computing FPTS from unrounded values, so reconstruction carries up to ~0.3 of
rounding error. My first version used a tight absolute tolerance and failed on
Christian McCaffrey at 0.23. The claim worth testing is which hypothesis fits
best, and half-PPR wins by more than 4×.

**Trap 3: the same player appears in two position files.**

Ingesting RB then TE raised a UNIQUE violation. The cause was real data, not a
bug in the store:

| Player | RB file | TE file |
|---|---|---|
| Connor Heyward (LV) | 0.50 | 0.54 |
| Riley Nowakowski (PIT) | 0.50 | 0.39 |
| Max Bredeson (MIN) | 0.35 | 0.18 |

FantasyPros projects these fullback/H-back types at two positions with
different numbers, and **both are real** — they describe different usages.
Keying on name alone made one file silently overwrite the other. Subject keys
now carry team and position.

Worth noting *how* this was found: the store's append-only UNIQUE constraint
caught what the adapter had missed. Enforcement at the storage layer earned
its keep — application-level dedup would have had no reason to look.

### Season averages are not weekly projections

These exports are per-game averages for the whole season — Hurts at 19.9
whether he faces the best or worst pass defense. That is a prior, not a slate
projection, and filing it as one would be precisely the plausible-looking
corruption this project keeps guarding against.

They are therefore stored under a **distinct metric**,
`projection_season_avg_dk_points`, with a test asserting they never leak into
the weekly `projection_dk_points` series. When FantasyPros' weekly export
appears in September, only the metric name changes.

**The underlying reason both vendors looked thin: it is August 17 and the
season starts September 10.** Nobody publishes weekly projections yet. DFF's
nine-row export and FantasyPros' season averages are the same fact wearing two
costumes. Neither vendor is broken; the calendar is.

### Two exports deliberately refused

- **Kickers.** DraftKings NFL Classic has no kicker slot, so those ~40 players
  could never appear in a lineup.
- **FLEX.** It duplicates the RB, WR and TE files; ingesting both would record
  two projections for every flex-eligible player.

Both refusals name their reason in the error, because a silent skip would look
identical to a successful capture.

459 tests, 99% coverage.

### DraftKings draftables adapter — and a heuristic that picked the wrong contest

`dfs-snapshot --slate-api` captures a slate directly. Live: 716 entries, 12
games, 6,284 observations, auto-selecting draft group 151307.

**Golden equivalence VERIFIED.** Against real captures of the same slate on
both sides — 716 players, **zero salary conflicts**, and every shared field
(name, position, salary, team, game, roster slots, kickoff, status) identical.
Downstream code cannot tell which path produced a slate, which is the point.

### The safety boundary is enforced by a test, not a promise

The adapter must never authenticate, hold a session, or mutate anything.
`test_no_authentication_code_exists` reads the module's own source and fails if
`requests.post`, `requests.Session`, `cookiejar`, `password`, `authorization`
or `bearer` appear in the body. A convention nobody checks is a convention that
erodes; a future edit adding a login should be hard to make by accident.

No session object at all — a session persists cookies, which is the first step
toward carrying an identity. Plain per-call GETs make that impossible. The
User-Agent identifies the tool honestly rather than impersonating a browser,
and there is a test asserting it contains no browser string. Every failure
message names the manual CSV import, because that path is a first-class equal,
not a degraded mode.

### The FLEX duplication trap

The draftables response carries **1,317 rows for 716 players**. The excess is
exactly 153 RB + 293 WR + 155 TE = 601, because DraftKings issues a *separate*
`draftableId` per roster slot:

```
Jahmyr Gibbs  draftableId=43727325  rosterSlotId=67 (RB)    salary=8000
Jahmyr Gibbs  draftableId=43727326  rosterSlotId=70 (FLEX)  salary=8000
```

Treating rows as players would invent 601 phantoms and could build a lineup
containing the same person twice under two ids — contest-illegal, and
invisible to every constraint check, because the solver sees two distinct ids.
Rows are grouped by `playerDkId`, and the **position-slot** id is chosen as
primary because that is the one the CSV carries; picking the FLEX id would make
the two paths permanently irreconcilable.

### A live run caught a heuristic guessing at something DraftKings states

The first live run **failed**, and correctly. Auto-selection chose draft group
146163 — 16 games, largest available — and every one of its 4,501 draftables
had **no salary field at all**.

146163 is a **Sit & Go: a snake draft.** Players are drafted in turns, not
priced, so there are no salaries. My heuristic ("largest non-simulated
multi-game slate") inferred contest format from game count and name, when the
lobby payload labels it outright:

| GameTypeId | Format |
|---|---|
| **1** | **Classic salary cap** |
| 96 | Showdown (single game) |
| 145 | Sit & Go (snake draft, no salaries) |
| 158 / 159 | Madden Stream (simulated) |

Selection now requires `GameTypeId == 1`, and auto-selection lands on 151307.
Defence in depth: a missing salary no longer reports "missing name, position or
salary" but names the likely cause — a draft-style contest — turning a
confusing parse error into an actionable one.

The lesson is worth stating plainly: **I inferred a structural property that
the data declared explicitly.** The fixture could never have caught this,
because I built the fixture from the slate I had already chosen correctly by
hand.

### A test-design defect worth recording

Four capture tests **hit the live DraftKings endpoint**. `ingest_slate` calls
`raw_bytes()`, which on the real adapter fetches. A suite that reaches the
network fails offline, hammers someone else's servers, and silently changes
behaviour week to week as slates roll over.

Fixed with an `OfflineApiAdapter` subclass that replaces only fetching, keeping
every line of parsing and validation under test. A guard test now monkeypatches
`requests.get` to raise, so the regression cannot recur silently.

### What this path knows that the CSV cannot

- **`playerDkId`** — a stable player identifier. `draftableId` is reissued
  every slate; this is not. It is what the Phase 1 crosswalk should key on, so
  a resolution made in Week 3 stays valid in Week 12 by id rather than by name.
- **Lock times as unambiguous UTC** (`2026-09-13T17:00:00Z`, `20:25:00Z`)
  rather than the CSV's `01:00PM ET`, which must be parsed and resolved across
  the daylight-saving boundary.
- Draft group id and stable `competitionId` values.

One parsing wrinkle: DraftKings writes **seven-digit** fractional seconds,
which `datetime.fromisoformat` rejects. Truncated to six, with a test for each
timestamp shape encountered.

510 tests, 99% coverage. No test touches the network.

### Both UNVERIFIED items closed — one by artifact, one by inference

**Item 1: the bulk-upload format. VERIFIED 2026-08-17.**

A real DraftKings upload template settled it. The file serves two purposes side
by side: entry columns on the left, the salary listing on the right.

```
cols 0-8   QB,RB,RB,WR,WR,WR,TE,FLEX,DST     <- the lineup you fill in
col  9     (blank spacer)
col  10+   Position,Name + ID,Name,ID,...    <- the listing to copy from
```

The prototype's assumed header was **exactly right**. Three things the template
settled that were not knowable without it:

- **`Name (ID)` is accepted.** DraftKings' instruction 2: *"you can use the
  Name + ID column or the ID column"*, and that column's values are literally
  `C.J. Stroud (43837771)`. Instruction 4 rules out a bare name.
- **A 500-lineup-per-file cap** we did not know existed. The writer now refuses
  before writing rather than after upload, which is the worst time to learn it.
- **One id per player serves every slot, including FLEX.** The salary block
  lists each player once with a combined `RB/FLEX` roster position and a single
  id. The draftables API *does* issue a separate FLEX `draftableId`, so using
  it here would have been a plausible mistake with no way to notice short of a
  failed upload.

**Item 2: reproducing a DraftKings-published player total. Substantially
verified, without a contest entry.**

The insight: **`AvgPointsPerGame` in a salary export is DraftKings' own
fantasy-point arithmetic.** Reproducing it from raw nflverse stat lines checks
our scoring against DK's actual *output*, not merely its published *rules*.

Restricting to players who appeared in all 17 regular-season games — so the
denominator cannot be ambiguous — and comparing:

| Window | Playoff players: median diff | Within 0.05 |
|---|---|---|
| Regular season only | 0.229 | 2/20 |
| **Regular + postseason** | **0.019** | **18/20** |

That is not a tuning exercise; it *discovered a DraftKings convention*.
**DraftKings includes playoff games in the average.** Across all 58 full-season
players on the slate: median absolute difference **0.023**, with **56 of 58**
inside DK's own two-decimal rounding — which is the floor of what any correct
implementation could achieve.

An initial hypothesis was wrong and worth recording: I first guessed the
residual came from DK dividing by more games (counting inactive weeks), and
tested whether `sum(points) / DK_average` landed on clean integers. Only 17%
did — because DK rounds the published average, which destroys the integer
signal. The test was defeated by the data's precision, not by the hypothesis
being unfalsifiable. Isolating full-season players removed the denominator
question entirely and gave a clean answer.

**What this does NOT verify, stated plainly.** An average over 17+ games
exercises the common path thoroughly — yardage, touchdowns, receptions, the
300/100 bonuses, interceptions, fumbles. It cannot exercise rare events:
safeties, two-point conversions, return touchdowns, offensive fumble-recovery
touchdowns. Those remain covered only by component tests against the published
rules. A single real contest box score would close that gap, which is why the
item is *substantially* verified rather than finished.

The fixture stores **raw per-game stat lines**, not stored answers, so the test
recomputes and a scoring regression fails it. A companion test proves the
postseason finding rather than asserting it: truncating to 17 games must worsen
agreement for *every* playoff player, and does.

554 tests, 99% coverage.

### Phase 1 begins: the identity crosswalk

The prototype's defining defect. It exact-matched lowercased names and, on a
miss, silently substituted a season average -- plausible lineups built on the
wrong numbers, with no signal anything had happened.

**Result on the real 692-player slate: 98.0% resolved, and zero unresolved
players above $5,000.**

### The handoff's central assumption was wrong

The specification calls for mapping "DK player IDs onto stable nflverse IDs"
via the nflverse crosswalk. **nflverse carries no DraftKings id.**
`ff_playerids` cross-references MFL, Sportradar, FantasyPros, PFF, Sleeper,
ESPN, Yahoo, CBS, PFR and Rotowire. DraftKings appears in none of them.

So DraftKings can only be joined by *name*, with team and position as
disambiguators. That makes the **persistent** crosswalk more important than
anticipated rather than less: a name match is expensive and fallible, so it is
made once and stored against DraftKings' stable `playerDkId`. Week 12 reuses
Week 3's answer by id. A test starves the resolver of its entire reference and
confirms a stored answer still stands.

### Layering two reference tables

| reference | resolved | unmatched |
|---|---|---|
| `ff_playerids` alone | 82.9% | 118 |
| `ff_playerids` + `players` | **98.0%** | 14 |

`ff_playerids` is preferred for its cross-platform ids; `players` is broader
(24k name keys against 8k) and catches rookies and fringe players the
fantasy-oriented table omits. The residue is 14, median salary $3,000, **none
above $5,000** — punt plays whose absence appears in the match report rather
than silently.

### A bug that discarded exactly what the module exists for

Deduplicating the reference by `nflverse_id` looked obviously correct and was
obviously wrong.

`ff_playerids` calls him **"Kenneth Gainwell"**; `players` and DraftKings say
**"Kenny Gainwell"** — both gsis `00-0036919`. Registering the first spelling
marked the id as seen, so the second was skipped as a duplicate. **The dedup
was throwing away precisely the aliases the crosswalk exists to resolve**, and
it cost the one expensive miss on the slate ($5,200).

Now keyed on `(id, normalized_name)`, so every spelling is indexed pointing at
one id. Several rows for one player is not ambiguity, so they collapse before
the ambiguity check. Fixing this took 98.0% and removed the only
above-floor gap.

### Deliberately no fuzzy matching

The handoff permits fuzzy matching above a confidence threshold with team and
position agreement. Not implemented, and asserted absent by test.

Exact matching plus team/position disambiguation already reaches 98%, and the
residue is the cheapest players on the slate. Fuzzy matching there trades a
**visible** miss for an **invisible** wrong answer: a miss is reported, costs
one punt play, and is resolvable by hand once and stored forever, while a bad
fuzzy match silently attaches one player's history to another and looks
exactly like success.

Revisit if a later slate shows expensive players in the residue — which is why
the residue is reported rather than hidden.

Related, and kept: a space is not treated as punctuation. `Ja'Marr` and
`JaMarr` both normalize to `jamarr`; `Ja Marr` deliberately does not match.
Deleting apostrophes is safe because sources disagree about them constantly;
collapsing spaces would merge genuinely distinct names.

### Defenses never touch name logic

Resolved through the exhaustive 32-team map, with a test confirming a defense
named after a quarterback resolves to nothing rather than matching him. A
defense is a team-level entity, not a player with an odd name.

### nflverse is a third team-abbreviation convention

`GBP`, `KCC`, `NEP`, `NOS`, `SDC`, `TBB` — added as aliases. Also `FA` and
`FA*` for unrostered players, which are **not** franchises and must not be
aliased to one; `is_free_agent()` lets a caller tell "no team" from "we failed
to recognise this". All 38 nflverse codes now resolve or are identified as
free-agent markers.

### Misses and rejections are stored too

A fruitless lookup is recorded so it is not repeated every week, and a
resolution a human marks `rejected` is never silently re-derived — otherwise
their decision would be undone on the next run.

587 tests, 99% coverage. No test touches the network.

### Slot assignment: greedy replaced with bipartite matching

The prototype filled slots in order, took the first eligible player, and wrote
``None`` into anything it could not fill -- which the CSV writer emitted as a
blank cell, producing an upload file that looks complete and is rejected.

**The deeper defect was that greedy assignment is order-dependent and can fail
on assignable lineups.** Slots `[FLEX, WR, WR, WR]` with players
`[WR, WR, WR, RB]`: fill FLEX first, take a receiver, and two remain for three
WR slots. The prototype avoided this only because it happened to fill
dedicated slots before FLEX — correct by luck of the constraint structure, not
by construction, and silently broken by any future change to the slot list.

Replaced with **maximum bipartite matching** (Kuhn's augmenting-path
algorithm), which finds a perfect matching if and only if one exists. When a
slot finds its preferred player taken, it asks that slot to move rather than
giving up — which is exactly the step greedy lacks. At nine players the
algorithm is instant; the reason to use it is correctness.

### The proof the handoff asked for

> "Prove `assign_slots()` correct over every valid position multiset."

Done exhaustively: all three legal roster shapes × every distinct player
ordering — **32,760 assignments**, each checked valid slot by slot.

| shape | distinct orderings |
|---|---|
| QB1 RB2 WR4 TE1 DST1 | 7,560 |
| QB1 RB3 WR3 TE1 DST1 | 10,080 |
| QB1 RB2 WR3 TE2 DST1 | 15,120 |

Writing that test needed its own bug fixed: the distinct-permutation generator
tested `if not remaining` as its base case while decrementing counts in place,
so `remaining` never emptied and it yielded nothing. The assertion
`checked > 10_000` caught it — a test that silently ran zero iterations would
otherwise have passed as green.

A property test also cross-checks the matcher against
`is_legal_roster_shape()`: two independent statements of the same constraint,
one enumerating shapes arithmetically and one solving a matching problem,
required never to disagree.

### Unassignable means raise, never blank

Every failure path raises `UnassignableLineup` carrying the positions
involved. Nothing partial is ever returned, because a blank cell in an upload
file is worse than a refusal — the refusal is visible.

### Determinism

A lineup can have several valid assignments; with two tight ends either may
take TE and the other FLEX. Slots are processed in `SLOT_ORDER` and players
tried in input order, so identical input always produces an identical file. An
optimizer that emits a different file each run is not reproducible, and
reproducibility is an acceptance criterion.

Verified end to end: `assign_slots()` output feeds `write_upload_csv()`
directly, producing a valid DraftKings upload row.

608 tests, 99% coverage.

### Open items

Nothing is BLOCKED. Everything below can proceed now.

- **Rare-event scoring** — safeties, two-point conversions, return touchdowns
  and offensive fumble-recovery touchdowns are covered only by component tests
  against DraftKings' published rules, not against DK's own output. Closing
  this needs a **regular-season** contest export: nflverse carries no
  preseason data, so a preseason contest cannot supply our side of the
  comparison.
- **Realized ownership** — needs a contest export. Roadmap R-stage work,
  explicitly out of scope for Phases 0 and 1.
- **Contest-results parser** — deliberately unbuilt. Every real file so far
  has broken a guessed schema; there is no reason to expect this one differs.
- Benchmark simultaneous vs sequential multi-lineup solving at realistic scale
  (~500 players, 20–150 lineups). Phase 1.
- Select a license.
