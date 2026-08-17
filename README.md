# Constrained Portfolio Optimization Under a Salary Cap

**A production Python pipeline built for DraftKings NFL daily fantasy sports,
demonstrating mixed-integer optimization, multi-source data engineering, and
temporal data integrity.**

> **Project status: early.** This repository was initialized on 2026-08-17.
> The two scripts at the root are working prototypes that have run only on
> synthetic data. The engineering described below is in progress, and every
> claim in this README is labelled **VERIFIED**, **UNVERIFIED**, or **BLOCKED**
> so that nothing here overstates what currently exists. See
> [DEVLOG.md](DEVLOG.md) for the dated build narrative.

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
| `DEVLOG.md` | Dated build log: what was built, what broke, what was decided and why. | Current |

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

## Tests

```bash
uv run pytest --cov=dfs_pipeline --cov-report=term-missing
```

**VERIFIED (2026-08-17):** 554 tests, 100% statement coverage. The suite
includes property-based tests over generated inputs, malformed-input fixtures
asserting each failure names its file/row/column, and fixtures recorded from
real DraftKings and Odds API responses. No test touches the network — a suite
that spends real API quota is a suite people stop running.

## License

Not yet selected.
