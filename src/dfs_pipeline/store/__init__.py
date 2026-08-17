"""Bitemporal snapshot store.

Captured observations are append-only and carry two timestamps, so the store
can answer "what was knowable at time T" without leaking information that had
not yet reached us. Raw artifacts are kept verbatim alongside the database so
that a parser fixed later can be re-run against data that cannot be
re-downloaded.
"""

from dfs_pipeline.store.database import (
    AppendOnlyViolation,
    Observation,
    SnapshotStore,
    StoreError,
    UnknownMetric,
    normalize_timestamp,
    utc_now,
)
from dfs_pipeline.store.schema import SCHEMA_VERSION, SEED_METRICS

__all__ = [
    "SnapshotStore",
    "Observation",
    "StoreError",
    "AppendOnlyViolation",
    "UnknownMetric",
    "normalize_timestamp",
    "utc_now",
    "SCHEMA_VERSION",
    "SEED_METRICS",
]
