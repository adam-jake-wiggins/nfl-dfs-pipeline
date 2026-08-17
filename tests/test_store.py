"""Tests for the bitemporal snapshot store.

The central test is `test_the_sunday_capture_trap`. Everything else supports
it. If that one passes and the rest fail, the design is sound; if it fails,
nothing else matters, because every backtest built on this store would be
reporting results that were never achievable.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
from hypothesis import given, strategies as st

from dfs_pipeline.store import (
    Observation,
    SnapshotStore,
    StoreError,
    UnknownMetric,
    normalize_timestamp,
    utc_now,
)

WED = "2026-09-09T12:00:00Z"
FRI = "2026-09-11T18:00:00Z"
SAT_AM = "2026-09-12T09:00:00Z"
SAT_PM = "2026-09-12T20:00:00Z"
SAT_CUTOFF = "2026-09-12T23:00:00Z"
SUN_AM = "2026-09-13T08:00:00Z"
SUN_CUTOFF = "2026-09-13T23:00:00Z"

PLAYER = "00-0034796"


@pytest.fixture()
def store(tmp_path):
    with SnapshotStore.open(tmp_path / "snapshots.sqlite") as s:
        yield s


@pytest.fixture()
def artifact(store):
    return store.put_artifact(
        b"raw,csv,content\n1,2,3\n",
        source="DFF",
        kind="projections_csv",
        original_filename="dff_week1.csv",
    )


def _proj(store, artifact, value, effective_at, captured_at):
    store.record(
        subject_type="player",
        source="DFF",
        source_subject_id=PLAYER,
        metric="projection_dk_points",
        value=value,
        effective_at=effective_at,
        captured_at=captured_at,
        artifact_sha256=artifact,
    )


# ---------------------------------------------------------------------------
# The reason this store exists
# ---------------------------------------------------------------------------

def test_the_sunday_capture_trap(store, artifact):
    """A record effective Saturday but captured Sunday must be invisible Saturday.

    This is the failure single-timestamp storage cannot prevent. The 21.5
    projection genuinely describes Saturday morning -- its effective_at is
    honest -- but it did not reach us until Sunday. A lineup decision made at
    Saturday 23:00 could not have used it. A backtest that sees it reports a
    result that was never achievable.
    """
    _proj(store, artifact, 14.2, WED, WED)
    _proj(store, artifact, 16.8, FRI, FRI)
    _proj(store, artifact, 17.1, SAT_PM, SAT_PM)
    # Effective Saturday morning; did not arrive until Sunday morning.
    _proj(store, artifact, 21.5, SAT_AM, SUN_AM)

    saturday = store.as_of(SAT_CUTOFF)
    assert len(saturday) == 1
    assert saturday[0].value == 17.1, "Sunday-captured record leaked into Saturday"

    # By Sunday all four are knowable. The answer is still 17.1, because
    # SAT_PM is the latest *effective* information -- the Sunday-captured
    # record describes an earlier moment (SAT_AM) and does not supersede it.
    sunday = store.as_of(SUN_CUTOFF)
    assert len(sunday) == 1
    assert sunday[0].value == 17.1
    assert sunday[0].effective_at == "2026-09-12T20:00:00Z"

    # And the late-arriving record is genuinely in the store -- it was
    # excluded from the Saturday view by timing, not by being dropped.
    assert store.observation_count() == 4


def test_filtering_on_effective_at_alone_would_have_leaked(store, artifact):
    """Demonstrates the bug this design prevents, by reproducing it directly."""
    _proj(store, artifact, 17.1, SAT_PM, SAT_PM)
    _proj(store, artifact, 21.5, SAT_AM, SUN_AM)

    naive = store._con.execute(
        "SELECT value_num FROM observation WHERE effective_at <= ? "
        "ORDER BY effective_at DESC",
        (normalize_timestamp(SAT_CUTOFF),),
    ).fetchall()
    assert 21.5 in [r[0] for r in naive], "the naive query should leak"

    correct = store.as_of(SAT_CUTOFF)
    assert 21.5 not in [o.value for o in correct], "the real query must not"


def test_as_of_walks_forward_correctly(store, artifact):
    _proj(store, artifact, 14.2, WED, WED)
    _proj(store, artifact, 16.8, FRI, FRI)
    _proj(store, artifact, 17.1, SAT_PM, SAT_PM)

    assert store.as_of("2026-09-10T23:00:00Z")[0].value == 14.2
    assert store.as_of("2026-09-11T23:00:00Z")[0].value == 16.8
    assert store.as_of(SAT_CUTOFF)[0].value == 17.1


def test_as_of_before_any_capture_returns_nothing(store, artifact):
    _proj(store, artifact, 14.2, WED, WED)
    assert store.as_of("2026-09-01T00:00:00Z") == []


def test_as_of_is_deterministic_on_tied_timestamps(store, artifact):
    """Two observations sharing both timestamps must resolve identically."""
    store.record(
        subject_type="player", source="DFF", source_subject_id=PLAYER,
        metric="projection_dk_points", value=10.0,
        effective_at=FRI, captured_at=FRI, artifact_sha256=artifact,
    )
    # Same subject/metric/timestamps but a different source -- legal, and
    # partitioned separately.
    store.record(
        subject_type="player", source="FantasyPros", source_subject_id=PLAYER,
        metric="projection_dk_points", value=20.0,
        effective_at=FRI, captured_at=FRI, artifact_sha256=artifact,
    )
    first = store.as_of(SAT_CUTOFF)
    for _ in range(5):
        assert store.as_of(SAT_CUTOFF) == first


# ---------------------------------------------------------------------------
# Append-only enforcement, by the database rather than by convention
# ---------------------------------------------------------------------------

def test_history_cannot_be_updated(store, artifact):
    _proj(store, artifact, 14.2, WED, WED)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._con.execute("UPDATE observation SET value_num = 99.9")


def test_history_cannot_be_deleted(store, artifact):
    _proj(store, artifact, 14.2, WED, WED)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._con.execute("DELETE FROM observation")


def test_duplicate_observation_is_rejected(store, artifact):
    _proj(store, artifact, 14.2, WED, WED)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _proj(store, artifact, 14.2, WED, WED)


def test_duplicate_can_be_ignored_when_explicitly_requested(store, artifact):
    """Re-running a partially failed capture is legitimate -- but opt-in."""
    _proj(store, artifact, 14.2, WED, WED)
    store.record(
        subject_type="player", source="DFF", source_subject_id=PLAYER,
        metric="projection_dk_points", value=14.2,
        effective_at=WED, captured_at=WED, artifact_sha256=artifact,
        on_duplicate="ignore",
    )
    assert store.observation_count() == 1


def test_same_effective_time_recaptured_later_is_a_distinct_record(store, artifact):
    """A revision arriving later is new history, not a duplicate."""
    _proj(store, artifact, 14.2, WED, WED)
    _proj(store, artifact, 15.9, WED, FRI)
    assert store.observation_count() == 2


# ---------------------------------------------------------------------------
# Constraints the database enforces itself
# ---------------------------------------------------------------------------

def test_foreign_keys_are_actually_enabled(store):
    """SQLite ships with foreign keys OFF; the store must turn them on.

    Without this pragma every REFERENCES clause in the schema is decorative,
    and an observation could cite an artifact that does not exist.
    """
    assert store._con.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_observation_cannot_cite_a_nonexistent_artifact(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.record(
            subject_type="player", source="DFF", source_subject_id=PLAYER,
            metric="projection_dk_points", value=14.2,
            effective_at=WED, captured_at=WED,
            artifact_sha256="0" * 64,
        )


def test_swapped_timestamps_are_rejected(store, artifact):
    """Passing effective_at and captured_at in the wrong order is a real bug.

    It is also invisible in the data afterwards, so the store refuses it.
    """
    with pytest.raises(sqlite3.IntegrityError):
        _proj(store, artifact, 14.2, SUN_AM, WED)


def test_small_clock_skew_is_tolerated(store, artifact):
    """Source clocks are not synchronised with ours; 30s ahead is not a bug."""
    _proj(store, artifact, 14.2, "2026-09-09T12:00:30Z", "2026-09-09T12:00:00Z")
    assert store.observation_count() == 1


def test_unregistered_metric_is_rejected(store, artifact):
    """A typo must not silently create a parallel metric."""
    with pytest.raises(UnknownMetric, match="projeciton_dk_points"):
        store.record(
            subject_type="player", source="DFF", source_subject_id=PLAYER,
            metric="projeciton_dk_points", value=14.2,
            effective_at=WED, captured_at=WED, artifact_sha256=artifact,
        )


def test_numeric_metric_rejects_text(store, artifact):
    with pytest.raises(StoreError, match="numeric"):
        store.record(
            subject_type="player", source="DFF", source_subject_id=PLAYER,
            metric="projection_dk_points", value="fourteen",
            effective_at=WED, captured_at=WED, artifact_sha256=artifact,
        )


def test_text_metric_rejects_numbers(store, artifact):
    with pytest.raises(StoreError, match="textual"):
        store.record(
            subject_type="player", source="DK", source_subject_id=PLAYER,
            metric="dk_position", value=7,
            effective_at=WED, captured_at=WED, artifact_sha256=artifact,
        )


def test_invalid_subject_type_is_rejected(store, artifact):
    with pytest.raises(sqlite3.IntegrityError):
        store.record(
            subject_type="coach", source="DFF", source_subject_id=PLAYER,
            metric="projection_dk_points", value=14.2,
            effective_at=WED, captured_at=WED, artifact_sha256=artifact,
        )


def test_batch_is_atomic(store, artifact):
    """One bad row must abort the whole batch, leaving no partial snapshot."""
    good = dict(
        subject_type="player", source="DFF", source_subject_id="A",
        metric="projection_dk_points", value=10.0,
        effective_at=WED, captured_at=WED,
    )
    bad = dict(good, source_subject_id="B", metric="not_a_real_metric")

    with pytest.raises(UnknownMetric):
        store.record_many([good, bad], artifact_sha256=artifact)
    assert store.observation_count() == 0, "partial batch was committed"


# ---------------------------------------------------------------------------
# Artifacts and provenance
# ---------------------------------------------------------------------------

def test_artifact_roundtrips_byte_for_byte(store):
    raw = b"Name,Proj\n\xc3\xa9t\xc3\xa9,12.5\n"  # non-ASCII on purpose
    sha = store.put_artifact(raw, source="DFF", kind="projections_csv")
    assert store.artifact_bytes(sha) == raw


def test_storing_identical_bytes_twice_is_idempotent(store):
    raw = b"same bytes"
    a = store.put_artifact(raw, source="DK", kind="salaries_csv")
    b = store.put_artifact(raw, source="DK", kind="salaries_csv")
    assert a == b
    assert store.artifact_count() == 1


def test_artifact_digest_is_verified_on_read(store, tmp_path):
    """Bit-rot in the raw zone must not pass silently."""
    sha = store.put_artifact(b"original", source="DK", kind="salaries_csv")
    path = tmp_path / "raw" / sha[:2] / f"{sha}.bin"
    path.write_bytes(b"tampered")
    with pytest.raises(StoreError, match="digest mismatch"):
        store.artifact_bytes(sha)


def test_reading_an_unregistered_artifact_fails_clearly(store):
    with pytest.raises(StoreError, match="no artifact registered"):
        store.artifact_bytes("a" * 64)


def test_every_observation_traces_to_its_artifact(store, artifact):
    _proj(store, artifact, 14.2, WED, WED)
    obs = store.as_of(SAT_CUTOFF)[0]
    assert obs.artifact_sha256 == artifact
    assert store.artifact_bytes(obs.artifact_sha256).startswith(b"raw,csv")


# ---------------------------------------------------------------------------
# Timestamp normalisation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "given_value, expected",
    [
        ("2026-09-12T20:00:00Z", "2026-09-12T20:00:00Z"),
        ("2026-09-12T20:00:00+00:00", "2026-09-12T20:00:00Z"),
        ("2026-09-12T16:00:00-04:00", "2026-09-12T20:00:00Z"),
        ("2026-09-12T20:00:00", "2026-09-12T20:00:00Z"),
        (datetime(2026, 9, 12, 20, 0, tzinfo=timezone.utc), "2026-09-12T20:00:00Z"),
    ],
)
def test_timestamps_normalize_to_utc(given_value, expected):
    assert normalize_timestamp(given_value) == expected


def test_unparseable_timestamp_fails_loudly():
    with pytest.raises(StoreError, match="unparseable"):
        normalize_timestamp("last Tuesday")


def test_canonical_format_sorts_chronologically():
    """String ordering must equal time ordering, or the as-of index lies."""
    stamps = [
        normalize_timestamp(s)
        for s in ("2026-09-09T12:00:00Z", "2026-01-01T00:00:00Z",
                  "2026-12-31T23:59:59Z", "2026-09-09T12:00:01Z")
    ]
    assert sorted(stamps) == sorted(
        stamps, key=lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
    )


@given(
    offset_hours=st.integers(min_value=-11, max_value=12),
    year=st.integers(min_value=2020, max_value=2035),
    month=st.integers(min_value=1, max_value=12),
    day=st.integers(min_value=1, max_value=28),
)
def test_normalization_is_idempotent(offset_hours, year, month, day):
    """Normalising twice must equal normalising once."""
    dt = datetime(year, month, day, 12, 0, tzinfo=timezone.utc)
    once = normalize_timestamp(dt)
    assert normalize_timestamp(once) == once


# ---------------------------------------------------------------------------
# Store lifecycle
# ---------------------------------------------------------------------------

def test_store_reopens_with_history_intact(tmp_path, ):
    path = tmp_path / "snapshots.sqlite"
    with SnapshotStore.open(path) as s:
        sha = s.put_artifact(b"x", source="DK", kind="salaries_csv")
        s.record(
            subject_type="player", source="DK", source_subject_id="1",
            metric="dk_salary", value=7800, effective_at=WED,
            captured_at=WED, artifact_sha256=sha,
        )
    with SnapshotStore.open(path) as s:
        assert s.observation_count() == 1
        assert s.as_of(SAT_CUTOFF)[0].value == 7800.0


def test_schema_version_is_recorded(store):
    assert store.schema_version() == 1


def test_opening_a_missing_store_without_create_fails(tmp_path):
    with pytest.raises(StoreError, match="no store at"):
        SnapshotStore.open(tmp_path / "nope.sqlite", create=False)


def test_filters_narrow_the_result(store, artifact):
    store.record_many(
        [
            dict(subject_type="player", source="DFF", source_subject_id="A",
                 metric="projection_dk_points", value=10.0,
                 effective_at=WED, captured_at=WED),
            dict(subject_type="team", source="OddsAPI", source_subject_id="KC",
                 metric="game_total", value=48.5,
                 effective_at=WED, captured_at=WED),
        ],
        artifact_sha256=artifact,
    )
    assert len(store.as_of(SAT_CUTOFF)) == 2
    assert len(store.as_of(SAT_CUTOFF, source="OddsAPI")) == 1
    assert len(store.as_of(SAT_CUTOFF, metric="game_total")) == 1
    assert len(store.as_of(SAT_CUTOFF, subject_type="player")) == 1
