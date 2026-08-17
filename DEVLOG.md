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

### Open items

- Verify DK Classic scoring constants against DraftKings' published rules
  (**BLOCKED** — needs the current published rules page).
- Verify the bulk-upload CSV format against a real DK entries template
  (**BLOCKED** — needs slates to open).
- Benchmark simultaneous vs sequential multi-lineup solving at realistic scale
  (~500 players, 20–150 lineups).
- Select a license.
