# DFS Research Roadmap (companion to CLAUDE_CODE_HANDOFF_v2)

This document is the destination map. It is deliberately separated from
the Code handoff so research ambition cannot bloat the engineering scope.
Nothing here is buildable until the Phase 0 harness has been collecting
weekly snapshots.

## The reframe both research passes converged on

We are not building "an optimizer." We are building toward a decision
engine: slate data -> player intelligence -> market intelligence ->
projection distributions -> ownership -> correlation -> simulation ->
portfolio construction. The optimizer is the LAST component, and the only
one built so far.

But the full stack is what commercial platforms employ teams to maintain.
The commitment is incremental: each stage must prove value on captured
data before the next stage starts. Stages that fail their test get
dropped, not polished.

## Guiding principle: inputs over answers

We do not want the best single point-projection API. We want the best
collection of information from which to derive our own distributions.
Target shape per player: P10/P25/P50/P75/P90/P95, not one number. How
that distribution interacts with salary and ownership is the actual GPP
question.

## Data layer status

| Layer | Source | Cost | Status |
|---|---|---|---|
| DK slates/salaries | Unofficial endpoints + CSV fallback | $0 | Phase 0 build |
| Historical NFL data | nflverse (nflreadpy) | $0 | Phase 0 build |
| Projections (free) | Daily Fantasy Fuel, FantasyPros API | $0 | Phase 0 build |
| Odds (current) | The Odds API Professional | $29/mo | Phase 0 build |
| Odds (historical + props) | The Odds API Business | $99/mo | Deferred: Stage R3 |
| Prop-derived market projections | SportsGameOdds or Odds API Business | TBD | Deferred: Stage R3 |
| Structured NFL feed | SportsDataIO (verify current pricing; Discovery Lab claim: $99/mo, free prior-season tier, 1-day-delayed dev data) | ~$99/mo | Deferred: Stage R4, only if a proven gap demands it |
| Independent projections | Fantasy Nerds (~$400/yr) | Deferred | Only if ensemble stage is reached |
| Sim platforms | SaberSim trial, RotoGrinders SimLabs free test | ~$7 | Stage R2 experiment |

## Research stages

### R1: Benchmark (weeks 1-4 of season, $0 beyond Odds API)
- Run our pipeline weekly. Snapshot everything.
- Cross-validate our optimizer against RotoWire's free optimizer using
  identical projections (divergence = a bug in one of us) and against
  pydfs-lineup-optimizer.
- Enter minimum-stakes contests to obtain contest-results exports for
  realized ownership.

### R2: Simulation gap analysis (~$7)
- SaberSim 7-day trial + RotoGrinders SimLabs free lineup, same slate as
  our system, same base projections where possible.
- Question is NOT "whose lineup is better" (unanswerable in one week).
  Question is: what does their construction differ on, and which
  mechanism (distribution shape, correlation, ownership leverage, field
  modeling) explains each difference?
- Input: Code's one-page assessment of chanzer0/NFL-DFS-Tools field
  simulation.
- Output: a written list of mechanisms our system lacks, ranked by
  plausible impact.

### R3: Market signal test (first paid escalation, ~$70/mo delta)
- Odds API Business (or SportsGameOdds; pick after checking prop coverage
  and point-in-time snapshot support against their five-minute-interval
  historical claims).
- Test: does model-market disagreement predict DK scoring? Build
  prop-implied stat lines for covered players; regress realized points on
  (projection, market-implied, disagreement).
- Honest constraint: props cover roughly the top 100-150 slate players
  and are heavily vigged. Market-derived projections are a FEATURE for
  the top of the pool, structurally silent on the punt plays that win
  GPPs. Any design that treats them as the projection engine is wrong.

### R4: Ensemble / model (multi-season horizon)
- With one-plus seasons of snapshots: test whether combining sources
  beats the best single source out of sample.
- Weights are LEARNED on past slates, evaluated on unseen slates — and
  with ~17 main slates/season and highly correlated sources, expect
  equal-weight averaging to be hard to beat. Regression-weighted
  ensembles are a season-two-plus project. No invented weights, ever.
- Only here does SportsDataIO's structured feed (snap counts, depth
  charts, historical projections) become a purchase candidate, and only
  if R1-R3 identified a gap it fills.

## Evaluation criteria (fixed now so they can't be gamed later)
Separate scores for separate questions:
- Projection accuracy: MAE/RMSE and rank correlation vs realized DK points.
- Tail accuracy: hit rate identifying 3x/4x/5x-salary outcomes.
- Ownership accuracy: vs realized contest ownership.
- Leverage: does (projection, ownership) jointly identify positive-ROI
  plays?
- Portfolio: contest-simulated ROI of the lineup SET, by contest type.
  A 20-max and a 150-max do not share an optimal portfolio; entry count
  and payout curve are inputs, not footnotes.

## Standing cautions
- Preseason validates plumbing only. Starters play a series; calibrate
  nothing on it.
- DFS review/comparison content is affiliate-monetized, including the
  odds-API comparison posts. Verify pricing on vendor pages before
  purchase decisions.
- Point-in-time discipline everywhere: a Week 7 backtest may not know
  anything after its own decision timestamp.
- Every paid escalation requires a named gap that the previous stage
  demonstrated. "Might help" is not a gap.
