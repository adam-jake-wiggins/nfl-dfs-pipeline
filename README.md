# Constrained Portfolio Optimization Under a Salary Cap

**A production Python pipeline built for DraftKings NFL daily fantasy sports,
demonstrating mixed-integer optimization, multi-source data engineering, and
temporal data integrity.**

> **Project status.** Repository initialized 2026-08-17. The capture and
> optimization pipeline is built and **verified against real DraftKings,
> FantasyPros, Daily Fantasy Fuel, Odds API and nflverse files** — 710 tests,
> 98% coverage. It has **not yet run for a full season**, and no contest
> results or ROI are claimed.
>
> The two scripts at the repository root are the original prototypes, kept
> unmodified so the hardening is visible as a diff rather than asserted.
>
> Every claim here is labelled **VERIFIED**, **UNVERIFIED**, or **BLOCKED**, so
> nothing overstates what exists. [PORTFOLIO_CASE_STUDY.md](PORTFOLIO_CASE_STUDY.md)
> is the narrative writeup; [DEVLOG.md](DEVLOG.md) is the dated build log,
> including the wrong turns.

---

## The problem

Each week, a DraftKings NFL Classic contest requires selecting nine players
under a $50,000 salary cap, subject to positional requirements, a
minimum-two-games rule, and correlation strategy. That is a textbook
mixed-integer programming problem — but it is wrapped in a considerably
messier data problem:

- **Three upstream sources with no shared player identifier.** Salaries,
  projections, and betting odds each use their own naming conventions.
  "Kenneth Walker III", "Kenneth Walker", and "K. Walker" are the same person
  to a human and three different people to a string comparison.
- **Schemas that change without notice.** The salary source is an unofficial,
  unsupported endpoint with no stability guarantee.
- **Point-in-time data that is unrecoverable if not captured.** Projections and
  odds as they stood on Saturday night cannot be bought or reconstructed on
  Monday. Any week not captured is gone permanently.

The last constraint drives the build order: data capture ships before
optimization hardening, because capture has a calendar deadline and hardening
does not.

## Repository contents

| Path | What it is | Status |
|---|---|---|
| `dk_optimizer.py` | MILP lineup optimizer (PuLP/CBC). Enforces contest rules, stacking, bring-back, exposure caps, locks, bans. | Prototype, synthetic data only |
| `dk_vegas_adjust.py` | Reweights projections by betting-market implied team totals. | Prototype, synthetic data only |
| `RESEARCH_ROADMAP.md` | The staged research plan, with explicit evidence gates on every paid escalation. | Current |
| `PORTFOLIO_CASE_STUDY.md` | The narrative writeup: the problem, what the real files corrected, what I got wrong, and the decisions rejected with their evidence. | Current |
| `DEVLOG.md` | Dated build log: what was built, what broke, what was decided and why. | Current |
| `TESTING.md` | What the test suite proves, how to run it, and what it does not prove. | Current |

Both scripts are committed here **in their original prototype form**, before
any hardening. That is deliberate: the commit history is intended to show the
real evolution of the work, not a polished endpoint presented as if it arrived
that way.

## Design decisions

### Mixed-integer formulation, not a greedy heuristic

Lineup construction is expressed as a binary integer program and solved with
CBC. Every returned lineup is provably optimal for the constraints given, not
a locally-good approximation. Roster shape is encoded as two-sided position
bounds plus a cardinality constraint:

```
sum(x) == 9,  QB == 1,  DST == 1,  2 <= RB <= 3,  3 <= WR <= 4,  1 <= TE <= 2
```

**VERIFIED (2026-08-17):** this feasible set was proved equivalent to the
independently-written rules in `DimaKudosh/pydfs-lineup-optimizer`
(commit `429db96`), which models the same contest with explicit roster slots
rather than aggregate counts. Two different formulations, same admissible
lineups. See [DEVLOG.md](DEVLOG.md) for the proof.

### Adapter isolation for the unstable dependency

**VERIFIED (2026-08-17)** for the CSV and odds paths. Each upstream source
lives behind one adapter class producing normalized records, so downstream code
never learns which source produced the data. When an endpoint changes, one
module changes.

Three sources spell teams three ways — `SEA`, `Seattle Seahawks`, `Seahawks` —
so `dfs_pipeline.teams` resolves all of them exhaustively. With exactly 32
teams there is no fuzzy matching at all: an unknown team raises rather than
guesses, because a silently unresolved team would drop half a game's odds while
the pipeline looked healthy.

No code in this repository authenticates as a user, submits entries, or
performs any account mutation. That is a hard boundary, not a current
limitation.

### Bitemporal timestamps as a schema requirement

**VERIFIED (2026-08-17).** Every captured observation carries two timestamps,
neither ever overwritten:

- `effective_at` — when the source says the information was current
- `captured_at` — when this system actually obtained it

These differ, and the difference matters. A record can describe Saturday's
information yet not have reached us until Sunday. Reconstructing "what was
knowable at Saturday 11 PM" requires both to fall at or before the cutoff.
Without this, a backtest silently leaks future information and reports a
result that was never achievable.

### Fail loud, never silent

The prototypes' most serious defect is a silent one: when a projection name
fails to match a salary name, the player quietly falls back to a season
average and the pipeline reports success. Silent degradation is treated as the
worst failure class in this project — worse than a crash, because a crash gets
fixed.

## Installation

Requires Python 3.11+. Using [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/adam-jake-wiggins/nfl-dfs-pipeline.git
cd nfl-dfs-pipeline
uv sync --extra dev
```

## Usage

Capture the slate straight from DraftKings (read-only, unauthenticated):

```bash
uv run dfs-snapshot --slate-api --odds
```

Or from a manually downloaded CSV — a fully supported equal, proven to
produce identical records by golden test:

```bash
uv run dfs-snapshot --salaries DKSalaries.csv --projections DFF_export.csv --odds
```

When both a slate and projections are supplied, every run prints a **match
report** — the share of slate players with a projection, and a warning
naming any player above $5,000 without one. The prototype's worst defect was
a silent name-match failure; a match rate nobody reports is a match rate
nobody checks.

Build upload-ready lineups from a slate and projections:

```bash
uv run dfs-optimize --salaries DKSalaries.csv \
    --projections DFF_export.csv \
    --lineups 20 --stack 2 --bringback 1 --max-exposure 0.4
```

Each run writes `lineups.csv` in DraftKings' bulk-upload format alongside a
match report, a validation report, and run metadata carrying every input's
SHA-256. Lineups are re-validated **after** the solve — roster shape, salary
cap, distinct games, no duplicate player — so a drift between the model and
the contest rules fails loudly rather than shipping.

Score a completed week from nflverse at DraftKings Classic rules:

```bash
uv run dfs-snapshot --results --season 2025 --week 1
```

Check remaining Odds API credits (costs nothing):

```bash
uv run dfs-snapshot --quota
```

Validate a file without writing anything:

```bash
uv run dfs-snapshot --salaries DKSalaries.csv --dry-run
```

Standing defaults live in `dfs.toml` (see `dfs.toml.example`); command-line
flags always win. `dfs-snapshot --show-config` prints the resolved values and
where each one came from.

Every run writes a self-contained directory under `runs/` holding the resolved
config, the SHA-256 of each input, timestamps, and the outcome — **including
when the run fails**, because a run that leaves no trace when it breaks cannot
be debugged afterwards.

Odds capture needs `ODDS_API_KEY` in the environment or `.env` (see
`.env.example`). The free tier bills **one credit per region per market**, so a
spreads-and-totals capture costs 2 — the adapter logs remaining quota on every
call and refuses to run below `--min-quota` (default 25), so a scheduled job
cannot exhaust the monthly budget before a live slate.

Exit codes: `0` success, `1` runtime failure, `2` usage error, `3` input data
rejected.

## Operator runbook

`RUNBOOK.pdf` is a one-page card covering the weekly routine — capture, build
lineups, score the week — plus a failure table keyed on the exact text the
tool prints. It is generated, not hand-written:

```bash
uv sync --extra docs
uv run python tools/make_runbook.py
```

Both the generator and the PDF are committed — the document is the visible
artifact, and requiring a build to see it defeats the point. Committing a
build output is only safe if something notices when it goes stale, so
`tests/test_runbook.py` pins three properties:

- the document builds to **exactly one page** — a silent spill onto page two
  loses whatever falls off the bottom;
- **every flag the runbook names still exists** in the live argument parsers,
  so renaming a flag breaks the suite rather than silently breaking the
  runbook;
- **the committed PDF still matches the generator**, compared on extracted
  text because reportlab stamps a creation date and no two builds are
  byte-identical.

Edit `tools/make_runbook.py`, forget to rebuild, and the suite goes red.

## Tests

```bash
uv run pytest --cov=dfs_pipeline --cov-report=term-missing
```

**VERIFIED (2026-08-19):** 710 tests, 98% statement coverage. The suite
includes property-based tests over generated inputs, malformed-input fixtures
asserting each failure names its file/row/column, and fixtures recorded from
real DraftKings and Odds API responses. No test touches the network — a suite
that spends real API quota is a suite people stop running.

See [TESTING.md](TESTING.md) for what the suite proves, the conventions it
follows, and — stated plainly — what it does not prove.

## License

Not yet selected.
