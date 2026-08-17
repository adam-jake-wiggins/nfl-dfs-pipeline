"""SQLite schema for the bitemporal snapshot store.

Design rationale
================

**Why SQLite.** Volume is small (~120k rows per season) so performance does
not decide this. What decides it: the point-in-time reconstruction query is
inherently relational, integrity can be enforced by the storage engine rather
than by convention, and the identity crosswalk needs mutable state with point
lookups. See DEVLOG.md for the full argument.

**Why a narrow observation table.** Observations are stored as
``(subject, metric, value)`` rather than one column per statistic. Upstream
sources add and rename fields without notice; under a wide schema every such
change is a migration. Here a new statistic is a new *row*, and the
point-in-time query is written and tested exactly once instead of once per
table.

The cost of that flexibility is normally the loss of a controlled vocabulary
-- typo ``projeciton`` and you have silently created a new metric. The
``metric`` table closes that hole: metric names are foreign keys, so an
unregistered name is rejected by the database, while *adding* a legitimate
metric stays a data insert rather than a schema migration.

**Bitemporality.** Every observation carries two timestamps and is never
updated or deleted:

``effective_at``
    When the source says the information was current -- the odds provider's
    last-update stamp, a projection's stated revision time.
``captured_at``
    When this system actually obtained it.

These differ, and the difference is the entire point. A projection can
describe Saturday morning yet not reach us until Sunday; a backtest deciding
at Saturday 23:00 must not see it. Reconstructing a past information state
therefore requires *both* timestamps to fall at or before the cutoff.
Filtering on one alone silently leaks future information and produces a
backtest result that was never achievable.

**Capture never blocks on identity resolution.** Observations key on
``(source, source_subject_id)`` -- whatever identifier the source itself used.
Mapping those onto stable nflverse identifiers happens later, in the
``crosswalk`` table. If a player cannot be resolved, the observation is still
captured. Losing data because a name did not match would be the worst
possible trade, since the data cannot be re-obtained.
"""

from __future__ import annotations

#: Bumped whenever SCHEMA_SQL changes in a way that requires migration.
SCHEMA_VERSION = 1

#: Tolerance, in seconds, for `effective_at` appearing to follow `captured_at`.
#: Source clocks are not synchronised with ours; a source stamping an event a
#: few seconds ahead of our wall clock is skew, not a data error. Anything
#: beyond this is a genuine mistake -- almost always the two columns swapped.
CLOCK_SKEW_TOLERANCE_SECONDS = 60

SCHEMA_SQL = f"""
-- ---------------------------------------------------------------------------
-- Migration bookkeeping
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Raw artifact registry
-- ---------------------------------------------------------------------------
-- Every observation is parsed from some raw file, and that file is kept
-- unmodified on disk. If a parser bug is discovered in December, the
-- September artifacts can be re-parsed; without them September is corrupt
-- permanently, and September cannot be downloaded again.
--
-- The SHA-256 is the primary key, which makes re-registering an identical
-- artifact idempotent and gives us the literal proof that a given file is
-- the one used on a given date.
CREATE TABLE IF NOT EXISTS artifact (
    sha256             TEXT PRIMARY KEY,
    source             TEXT NOT NULL,
    kind               TEXT NOT NULL,
    original_filename  TEXT,
    stored_path        TEXT NOT NULL,
    byte_size          INTEGER NOT NULL,
    retrieved_at       TEXT NOT NULL,

    CHECK (length(sha256) = 64),
    CHECK (sha256 = lower(sha256)),
    CHECK (byte_size >= 0),
    CHECK (length(source) > 0),
    CHECK (length(kind) > 0)
);

-- ---------------------------------------------------------------------------
-- Metric vocabulary
-- ---------------------------------------------------------------------------
-- A controlled vocabulary that can be extended without a schema migration.
-- `value_kind` declares which of observation.value_num / value_text a metric
-- is allowed to populate; enforced by trigger below, because SQLite CHECK
-- constraints cannot reference another table.
CREATE TABLE IF NOT EXISTS metric (
    metric       TEXT PRIMARY KEY,
    value_kind   TEXT NOT NULL,
    unit         TEXT,
    description  TEXT NOT NULL,

    CHECK (value_kind IN ('num', 'text')),
    CHECK (length(metric) > 0),
    CHECK (length(description) > 0)
);

-- ---------------------------------------------------------------------------
-- Observations: the bitemporal core. Append-only.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS observation (
    obs_id             INTEGER PRIMARY KEY,

    -- What the observation is about, in the source's own terms.
    subject_type       TEXT NOT NULL,
    source             TEXT NOT NULL,
    source_subject_id  TEXT NOT NULL,

    metric             TEXT NOT NULL REFERENCES metric(metric),
    value_num          REAL,
    value_text         TEXT,

    -- The two timestamps. Neither is ever overwritten.
    effective_at       TEXT NOT NULL,
    captured_at        TEXT NOT NULL,

    artifact_sha256    TEXT NOT NULL REFERENCES artifact(sha256),

    CHECK (subject_type IN ('player', 'dst', 'team', 'game')),
    CHECK (length(source) > 0),
    CHECK (length(source_subject_id) > 0),

    -- Exactly one value column is populated, never both, never neither.
    CHECK ((value_num IS NULL) <> (value_text IS NULL)),

    -- Timestamps must parse. julianday() returns NULL on unparseable input,
    -- so this rejects malformed stamps at insert rather than yielding
    -- silently-wrong results at query time.
    CHECK (julianday(effective_at) IS NOT NULL),
    CHECK (julianday(captured_at)  IS NOT NULL),

    -- Information cannot be captured meaningfully before it exists, allowing
    -- for clock skew between the source and us. Catches the classic bug of
    -- passing the two timestamps in the wrong order.
    CHECK (julianday(effective_at)
           <= julianday(captured_at) + {CLOCK_SKEW_TOLERANCE_SECONDS}.0 / 86400.0),

    -- Re-running a capture must not create duplicate history. Two genuinely
    -- distinct observations differ in at least one timestamp.
    UNIQUE (source, source_subject_id, metric, effective_at, captured_at)
);

-- The as-of query filters on both timestamps and partitions by subject and
-- metric; this index matches that access path.
CREATE INDEX IF NOT EXISTS idx_observation_asof
    ON observation (source, source_subject_id, metric, effective_at, captured_at);

CREATE INDEX IF NOT EXISTS idx_observation_capture
    ON observation (captured_at);

CREATE INDEX IF NOT EXISTS idx_observation_artifact
    ON observation (artifact_sha256);

-- ---------------------------------------------------------------------------
-- Append-only enforcement
-- ---------------------------------------------------------------------------
-- The append-only guarantee is the foundation of every point-in-time claim
-- this project makes. Enforcing it in application code means it holds only
-- as long as everyone remembers; enforcing it here means it holds always,
-- including from a SQLite browser at midnight in Week 12.
CREATE TRIGGER IF NOT EXISTS observation_is_append_only_update
BEFORE UPDATE ON observation
BEGIN
    SELECT RAISE(ABORT,
        'observation is append-only: record a new observation instead of updating');
END;

CREATE TRIGGER IF NOT EXISTS observation_is_append_only_delete
BEFORE DELETE ON observation
BEGIN
    SELECT RAISE(ABORT,
        'observation is append-only: history must not be deleted');
END;

-- A metric declared 'num' must not arrive as text, and vice versa. SQLite
-- CHECK constraints cannot reference another table, so this needs a trigger.
CREATE TRIGGER IF NOT EXISTS observation_value_kind_matches_metric
BEFORE INSERT ON observation
BEGIN
    SELECT RAISE(ABORT, 'value kind does not match the metric declaration')
    WHERE (
        SELECT value_kind FROM metric WHERE metric.metric = NEW.metric
    ) IS NOT (
        CASE WHEN NEW.value_num IS NOT NULL THEN 'num' ELSE 'text' END
    );
END;

-- ---------------------------------------------------------------------------
-- Identity crosswalk: mutable, unlike everything above
-- ---------------------------------------------------------------------------
-- Resolutions are stored rather than recomputed. Once an awkward player is
-- resolved -- by ID, by normalisation, or by a human decision -- next week's
-- run reuses that resolution instead of re-deriving it and possibly
-- re-deriving it differently.
CREATE TABLE IF NOT EXISTS crosswalk (
    crosswalk_id       INTEGER PRIMARY KEY,
    source             TEXT NOT NULL,
    source_subject_id  TEXT NOT NULL,
    source_name        TEXT,
    team               TEXT,
    position           TEXT,
    entity_type        TEXT NOT NULL,
    nflverse_id        TEXT,
    match_method       TEXT NOT NULL,
    confidence         REAL,
    review_status      TEXT NOT NULL DEFAULT 'pending',
    first_seen         TEXT NOT NULL,
    last_seen          TEXT NOT NULL,

    CHECK (entity_type IN ('player', 'dst')),
    CHECK (match_method IN
           ('id', 'normalized', 'fuzzy', 'manual', 'dst_alias', 'unresolved')),
    CHECK (review_status IN ('pending', 'approved', 'rejected')),
    CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),

    -- Fuzzy matches must carry a confidence; exact ones need not.
    CHECK (match_method <> 'fuzzy' OR confidence IS NOT NULL),
    -- An unresolved row must not claim an nflverse id.
    CHECK (match_method <> 'unresolved' OR nflverse_id IS NULL),

    UNIQUE (source, source_subject_id)
);

CREATE INDEX IF NOT EXISTS idx_crosswalk_nflverse ON crosswalk (nflverse_id);
CREATE INDEX IF NOT EXISTS idx_crosswalk_review   ON crosswalk (review_status);
"""

#: Metrics registered on store creation. Extending this list is a data
#: insert, not a schema migration -- but the vocabulary stays controlled,
#: so a typo'd metric name is rejected rather than silently created.
SEED_METRICS: tuple[tuple[str, str, str | None, str], ...] = (
    # --- DraftKings slate ---
    ("dk_salary", "num", "usd", "DraftKings salary for a player on a slate"),
    ("dk_position", "text", None, "Position as DraftKings lists it"),
    ("dk_roster_position", "text", None, "Roster-eligible slot string from DraftKings"),
    ("dk_team", "text", None, "Team abbreviation as DraftKings lists it"),
    ("dk_game", "text", None, "Game identifier, e.g. 'KC@BUF'"),
    ("dk_player_name", "text", None, "Player name exactly as DraftKings spells it"),
    ("dk_avg_points", "num", "points", "DraftKings AvgPointsPerGame field"),
    ("dk_lock_time", "text", None, "Slate lock time, ISO 8601 (API path only)"),
    ("dk_status", "text", None, "DraftKings injury designation: Q, IR, or OUT"),
    ("dk_stable_player_id", "text", None,
     "DraftKings playerDkId -- stable across slates, unlike draftableId (API path only)"),
    # --- Projections ---
    ("projection_dk_points", "num", "points", "Projected DraftKings points"),
    ("projection_ownership", "num", "percent", "Projected ownership percentage"),
    ("projection_season_avg_dk_points", "num", "points",
     "Season-long per-game average DK points -- a prior, NOT a weekly slate projection"),
    ("projection_source_name", "text", None, "Player name exactly as the projection source spelled it"),
    ("projection_position", "text", None, "Position as the projection source lists it"),
    ("projection_team", "text", None, "Team as the projection source lists it"),
    ("projection_injury_status", "text", None, "Injury designation as the projection source reports it"),
    # --- Betting market ---
    ("spread", "num", "points", "Point spread from the team's perspective; negative favours"),
    ("game_total", "num", "points", "Over/under for the game"),
    ("moneyline", "num", "american_odds", "Moneyline price for the team"),
    ("odds_team", "text", None, "Canonical team abbreviation this odds row is about"),
    ("odds_game", "text", None, "Game key in AWAY@HOME form, joinable to dk_game"),
    ("odds_commence_time", "text", None, "Scheduled kickoff, ISO 8601 UTC"),
    ("implied_team_total", "num", "points", "(game_total / 2) - (spread / 2)"),
    # --- Realized outcomes ---
    ("actual_dk_points", "num", "points", "Realized DraftKings points, computed at DK Classic scoring"),
    ("nflverse_name", "text", None, "Player or defense name as nflverse spells it"),
    ("nflverse_team", "text", None, "Canonical team abbreviation from nflverse"),
    ("nflverse_position", "text", None, "Position as nflverse lists it"),
    ("points_allowed", "num", "points", "DST-attributable points allowed (NOT the opponent final score)"),
    ("actual_ownership", "num", "percent", "Realized ownership from a contest-results export"),
)
