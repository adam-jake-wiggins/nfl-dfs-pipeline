# Constrained Portfolio Optimization Under a Salary Cap
### A production Python pipeline, built for DraftKings NFL daily fantasy sports — demonstrating mixed-integer optimization, multi-source data engineering, and temporal data integrity

**Repository:** https://github.com/adam-jake-wiggins/nfl-dfs-pipeline

---

## Summary

Weekly DFS contests are a textbook mixed-integer program wrapped in a genuinely
hostile data problem: three upstream sources with no shared identifier, schemas
that change without notice, and point-in-time data that is unrecoverable if not
captured the week it exists. I built a tested, reproducible pipeline that
captures that data with bitemporal integrity and turns it into contest-legal
lineups in one command.

The outcome I would put first is not a lineup. It is that **every schema
assumption in the original specification turned out to be wrong in a way that
would have produced plausible, wrong numbers** — and each was caught by a
strict reader failing loudly against a real file, rather than by a lenient one
guessing and moving on.

---

## The problem

Each week, a DraftKings NFL Classic entry is nine players under a $50,000 cap,
subject to positional requirements, a minimum-two-games rule, and correlation
strategy. That part is a binary integer program.

The hard part is everything upstream:

- **No shared player identifier.** Salaries, projections, odds and historical
  statistics each use their own naming. `Ja'Marr Chase`, `JaMarr Chase` and
  `Ja Marr Chase` are one person to a human and three to a string comparison.
  DraftKings issues an ID; **nflverse's cross-platform crosswalk does not carry
  it**, so the join has to be made by name and then *remembered*.
- **Undocumented, unstable sources.** The salary endpoint has no stability
  guarantee. Vendors rename columns between products and seasons.
- **Point-in-time data that expires.** A projection as it stood on Saturday
  night cannot be bought on Monday. Neither can realized contest ownership.
  Every week not captured is gone permanently.

That last constraint set the build order: **capture shipped before
optimization hardening**, because capture had a calendar deadline and hardening
did not.

---

## What I built

- **A bitemporal snapshot store** (SQLite) where every observation carries both
  when the source said it was current and when we obtained it, is append-only
  by database trigger, and can answer "what was knowable at Saturday 11 PM"
  without leaking information that had not yet arrived.
- **Six source adapters** — DraftKings CSV and API, two projection vendors,
  The Odds API, nflverse — each producing identical normalized records, with a
  golden test proving the two DraftKings paths are interchangeable.
- **An identity crosswalk** resolving source players onto stable nflverse IDs
  at **98.0%** on a real 692-player slate, persisting each decision so a name
  is never matched twice.
- **A MILP lineup optimizer** with contest constraints, stacking, exposure
  control and locks, whose output is re-validated after the solve and written
  in DraftKings' verified bulk-upload format.
- **705 tests at 98% coverage**, running in 42 seconds against recorded real
  files, touching no network.

Two commands: `dfs-snapshot` captures, `dfs-optimize` builds. Each writes a
self-contained run directory with input hashes, timestamps, and a report of
everything it dropped.

---

## Architecture

```
        capture                            build
  ┌───────────────────┐            ┌──────────────────┐
  │ DK CSV ─┐         │            │  pool filter     │
  │ DK API ─┼─ slate  │            │       ↓          │
  │ DFF   ──┼─ proj   │──▶ store ──▶  identity join   │
  │ FPros ──┘         │  (SQLite)  │       ↓          │
  │ Odds API ─ market │  append-   │  MILP solve      │
  │ nflverse ─ results│  only,     │       ↓          │
  └───────────────────┘  bitemporal│  slot assignment │
                                   │       ↓          │
                                   │  validate        │
                                   │       ↓          │
                                   │  upload CSV      │
                                   └──────────────────┘
```

Every arrow into the store carries two timestamps and a SHA-256 of the raw
artifact it came from. Raw bytes are kept verbatim beside the database, so a
parser bug found in December can be re-run against September's data.

### Design decisions worth narrating

**1. Adapter isolation for unstable dependencies.** Each source lives behind
one class producing the same normalized record. When DraftKings' undocumented
endpoint changes, one module changes. The manual CSV import is a **first-class
equal**, not a degraded fallback — proved by a golden test on real captures of
the same slate: 716 players, zero field disagreements.

**2. Bitemporality as a schema requirement, not a convention.** `effective_at`
and `captured_at`, neither ever overwritten, enforced by CHECK constraints and
append-only triggers. The reconstruction query requires *both* to precede the
cutoff. A companion test runs the naive single-timestamp query and **asserts
that it leaks**, so the bug is pinned in place and cannot be "simplified" back
in.

**3. Storage-layer enforcement over application discipline.** Integrity lives
in the database: UNIQUE constraints, CHECK constraints with clock-skew
tolerance, triggers rejecting UPDATE and DELETE. This paid for itself directly
— a UNIQUE violation caught a bug where three players appeared in two
FantasyPros position files with different projections, which application-level
dedup had no reason to look for.

**4. Strict readers, generous alias lists.** Column aliases are broad, but an
unrecognised schema is a hard failure that names every alias tried and every
header found. That is why a rejected file became a one-line fix rather than an
investigation — four separate times.

**5. Capture never blocks on resolution.** Observations key on the source's own
identifier; mapping to canonical IDs happens later. Losing a projection because
a name did not match would be unrecoverable, and no tidiness is worth that.

---

## What the real files taught me

Every assumption in the specification that touched a schema was wrong. None
would have crashed. All would have produced plausible numbers.

| Assumption | Reality | Consequence if trusted |
|---|---|---|
| DK emits `OUT,DOUBTFUL` | Emits `Q`, `IR`, `OUT` — never `DOUBTFUL` | The documented filter matches nothing; injured players stay in the pool |
| nflverse crosswalk carries a DK ID | It carries ten platforms' IDs; **not DraftKings** | The specified join strategy is impossible |
| One draftables row per player | A separate ID **per roster slot** — 1,317 rows for 716 players | 601 phantom players; the same person twice in one lineup, contest-illegal and invisible to every constraint check |
| FantasyPros `FPTS` is usable | **Half-PPR**; DraftKings is full PPR | Every pass-catcher under-projected by 0.5 × receptions — 3.66 points for Ja'Marr Chase |
| Projection column is `projection` | DFF uses `ppg_projection` | Reader rejects the file — correctly, and says which aliases it tried |
| Largest multi-game slate is the main slate | A 16-game **Sit & Go** is a snake draft with no salaries at all | Auto-selection picks a contest format the pipeline cannot use |
| Yardage bonuses are stepped | **Continuous**: 0.04/yard | `yards // 25` under-scores nearly every player, every slate |
| Points allowed = opponent's final score | DST-attributable only; a pick-six is **not** charged | Minnesota's defense scored one tier too low |

The pattern is consistent enough to be the lesson: **I inferred structural
properties that the data stated outright.** The Sit & Go was labelled
`GameTypeId: 145`. The FLEX duplication was visible in `rosterSlotId`. Guessing
felt like engineering and was not.

---

## What I got wrong

The mistakes are the interview material, so here they are unhedged.

**I wrote three tests that asserted the wrong invariant.** One checked that an
installed package imports from `site-packages` — false under an editable
install, so it would have *passed* under broken packaging and *failed* under
correct packaging. One accepted two contradictory outcomes with an `or`. One
forgot the 100-yard bonus I had implemented an hour earlier. Writing a test
from the same mental model that wrote the code reproduces the model's blind
spots. The defence that works is deriving expected values from the **source
document**, not from the implementation.

**A test claimed exhaustive coverage while running zero iterations.** My
distinct-permutation generator never terminated its recursion, so the
32,760-case proof silently checked nothing. Caught only by
`assert checked > 10_000` — an assertion about the test's own coverage. I now
write that assertion whenever a test claims to be exhaustive.

**Four tests hit the live DraftKings endpoint.** `ingest_slate` calls
`raw_bytes()`, which fetches. A suite that reaches the network fails offline
and changes behaviour week to week. Fixed with an offline subclass plus a guard
test that makes `requests.get` raise.

**My deduplication discarded exactly what the module existed for.** Building
the identity reference, I deduplicated by player ID — which meant
`ff_playerids`' "Kenneth Gainwell" claimed the ID and `players`' "Kenny
Gainwell" was skipped as a duplicate. The crosswalk exists to resolve alternate
spellings, and my dedup was throwing them away. It cost the single most
expensive unresolved player on the slate.

**I shipped a technically-accurate, useless error message.** The first
end-to-end optimizer run produced zero lineups from a 665-player pool and
reported "player pool too small or too constrained." The real cause was that
the projections file contained no defenses. Now it says
`DST: 0 in the pool, 1 required`. Every unit test passed throughout — only
running the command against real data exposed it.

---

## Results

Two ledgers, kept separate on purpose. **The engineering ledger stands on its
own even if the model never makes a dollar**, and that separation is what
protects this document.

### Engineering outcomes — all verifiable

| | |
|---|---|
| **Repository** | 18 commits, dated, each a logical unit with a real message |
| **Tests** | 705, 98% coverage (branch), 42 seconds, zero network calls |
| **Code** | ~7,100 lines of source, ~7,200 of tests, 32 recorded fixtures |
| **Identity resolution** | 98.0% on a real 692-player slate; zero unresolved above $5,000 |
| **Projection matching** | 99.7% on a real slate, with every gap above $5,000 named |
| **Scoring accuracy** | Reproduces DraftKings' own published per-game averages to a **median of 0.023 points** — inside DK's rounding |
| **Cross-validation** | Roster constraints proved equivalent to an independently written optimizer; scoring matches nflverse on 355/355 players |
| **Runtime** | 20 lineups in 24s; 150 in 3.6 minutes, on a 693-player pool |

### The exposure benchmark

The specification asked whether a simultaneous multi-lineup formulation was
tractable, and required runtime evidence either way. Both were implemented and
measured on the real pool:

| lineups | sequential | simultaneous |
|---:|---:|---:|
| 1 | 3.13s | 0.30s |
| 5 | 2.27s | **121.78s** |
| 20 | 10.54s | not attempted |
| 150 | **218.65s** | not attempted |

Simultaneous is provably optimal as a *set* and hit a two-minute wall at five
lineups — symmetry gives every solution N! relabellings, and pairwise
uniqueness needs an auxiliary variable per lineup-pair per player. **Sequential
was kept on evidence, with its cost stated plainly**: each lineup is optimal
given those already built, but the set is not jointly optimal.

### Modeling outcomes

**None yet, and that is the honest status.** No contest has been entered on a
completed regular-season slate, no backtest has run, and no ROI is claimed.
The capture harness has been collecting since before Week 1 specifically so
that backtests, when they come, will have point-in-time data that was never
reconstructed after the fact.

---

## Decisions rejected, and why

| Approach | Why considered | Why rejected | Evidence |
|---|---|---|---|
| Fuzzy name matching | The spec permits it above a confidence threshold | Exact matching plus team/position reaches 98%; the residue is the cheapest players on the slate. Fuzzy trades a **visible miss** for an **invisible wrong answer** | Match rate on a real slate; residue median salary $3,000 |
| Simultaneous multi-lineup MILP | Provably optimal as a set | 50× slower at N=5, unusable at N=20+ | Benchmark table above |
| Greedy slot assignment | Simple, and worked | Order-dependent; failed on assignable lineups, and was correct only by luck of the constraint order | Replaced with bipartite matching, proved over 32,760 cases |
| Trusting FantasyPros' `FPTS` | It is their published number | Half-PPR, not DraftKings scoring | Reconstructed from their own component stats |
| Vegas alpha = 0.50 by intuition | Plausible reasoning | No evidence either way; and the real DFF export carries `spread` and `implied_team_score`, so their projections already price the market — applying it again double-counts | Deferred until measurable |
| Excluding QUESTIONABLE players by default | Simpler pool | A questionable player is a judgement call and often the point of a contrarian lineup; the default would make that choice silently | Defaults to the genuinely unplayable only |
| Filtering injuries at capture | Smaller store | Status at capture time is point-in-time data; discarding it destroys what a backtest needs | Filtering lives in the optimizer |
| Building a projection model | The obvious next step | Explicitly out of scope; and it needs weeks of captured data before it can be evaluated honestly | Deferred to the research roadmap |

---

## Skills demonstrated

- **Operations research** — MILP formulation and constraint modelling; maximum
  bipartite matching for assignment; measured evaluation of solver tractability
  rather than assumed.
- **Data engineering** — multi-source ingestion behind adapter isolation;
  schema validation that fails loudly and specifically; identity resolution
  across four naming conventions; bitemporal modelling with append-only
  integrity enforced at the storage layer.
- **Software reliability** — 705 tests including property-based, golden-file
  and exhaustive proofs; malformed-input fixtures; structured logging; per-run
  audit directories with input hashing; packaging and console entry points.
- **Judgement under uncertainty** — every claim labelled VERIFIED, UNVERIFIED
  or BLOCKED; verification split where two claims were being conflated;
  evidence-gated spending, with no paid data source purchased because no
  demonstrated gap justified one.
- **Security and boundary discipline** — no code path authenticates as the
  operator, asserted by a test that reads the module's own source; secrets read
  from the environment and scrubbed from every error path.

---

## Honest limitations

- **Not yet run for a full season.** The harness works; a season of weekly runs
  is the evidence that matters and does not exist yet.
- **Rare scoring events** — safeties, two-point conversions, return touchdowns
  — are verified against DraftKings' published *rules* but not its *output*.
  Closing that needs a real contest box score.
- **No ownership data.** Realized contest ownership is a prerequisite for the
  field simulation the roadmap describes, and cannot be obtained without
  entering contests.
- **Projection quality is entirely unaddressed.** This pipeline moves numbers
  correctly; whether the numbers are any good is a separate question it makes
  no claim about.

---

### Framing note

Lead with the optimization and the data engineering; let daily fantasy sports
be the colourful context rather than the headline. The transferable problems
*are* the story — constrained resource allocation, identity resolution across
systems that share no key, and temporal data integrity translate directly to
scheduling, claims, and reporting work.

But do not overcorrect into evasion. A sanitised title on a repository that is
visibly DraftKings throughout reads as hiding something, which is worse than
the domain. Title carries the engineering, subtitle names the domain plainly.
What varies by audience is emphasis and placement — never disclosure.
