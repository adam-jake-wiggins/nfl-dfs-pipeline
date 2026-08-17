"""Bitemporal snapshot store, backed by SQLite.

The public surface is deliberately small:

    store = SnapshotStore.open(path)
    sha   = store.put_artifact(raw_bytes, source=..., kind=...)
    store.record(subject_type=..., source=..., ..., artifact_sha256=sha)
    rows  = store.as_of("2026-09-12T23:00:00Z")

Everything else is enforcement. See :mod:`dfs_pipeline.store.schema` for why
the schema looks the way it does.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from dfs_pipeline.store.schema import SCHEMA_SQL, SCHEMA_VERSION, SEED_METRICS

__all__ = [
    "SnapshotStore",
    "Observation",
    "StoreError",
    "AppendOnlyViolation",
    "UnknownMetric",
    "normalize_timestamp",
    "utc_now",
]

#: Canonical on-disk timestamp format. Chosen so that lexicographic string
#: ordering equals chronological ordering, which lets the as-of query use a
#: plain index instead of calling julianday() on every row.
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class StoreError(RuntimeError):
    """Base class for store-level failures."""


class AppendOnlyViolation(StoreError):
    """Raised on any attempt to update or delete recorded history."""


class UnknownMetric(StoreError):
    """Raised when recording an observation against an unregistered metric.

    This is deliberately an error rather than an auto-registration. Silently
    accepting a new metric name would make a typo indistinguishable from a
    deliberate addition, and the resulting split history would be invisible
    until someone noticed half the projections had vanished.
    """


def utc_now() -> str:
    """Current UTC time in the store's canonical format."""
    return datetime.now(timezone.utc).strftime(_TS_FORMAT)


def normalize_timestamp(value: str | datetime) -> str:
    """Convert a timestamp to the store's canonical UTC representation.

    Accepts a :class:`datetime` or an ISO 8601 string, with or without a
    timezone. Naive values are *assumed* to be UTC rather than silently
    interpreted as local time, because a store that shifts by the operator's
    timezone would corrupt every point-in-time claim it makes.

    Normalising on write is what allows the as-of query to compare timestamps
    as strings, which in turn is what lets it use an index.
    """
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise StoreError(f"unparseable timestamp: {value!r}") from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(_TS_FORMAT)


@dataclass(frozen=True, slots=True)
class Observation:
    """One resolved observation returned by a point-in-time query."""

    subject_type: str
    source: str
    source_subject_id: str
    metric: str
    value: float | str
    effective_at: str
    captured_at: str
    artifact_sha256: str


class SnapshotStore:
    """A bitemporal, append-only store of captured slate data."""

    def __init__(self, connection: sqlite3.Connection, root: Path) -> None:
        self._con = connection
        self._root = root

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(cls, path: str | Path, *, create: bool = True) -> SnapshotStore:
        """Open (and by default create) a store at ``path``.

        Raw artifacts are written to a ``raw/`` directory beside the database
        file, so the store is one self-contained, copyable directory.
        """
        path = Path(path)
        if not create and not path.exists():
            raise StoreError(f"no store at {path}")
        path.parent.mkdir(parents=True, exist_ok=True)

        con = sqlite3.connect(path, isolation_level=None)
        con.row_factory = sqlite3.Row

        # Foreign keys are OFF by default in SQLite. Without this every
        # REFERENCES clause in the schema is decorative.
        con.execute("PRAGMA foreign_keys = ON")
        # Write-ahead logging: readers do not block the writer, and a crash
        # mid-capture leaves a recoverable database rather than a torn one.
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = FULL")

        store = cls(con, path.parent / "raw")
        store._apply_schema()
        return store

    def _apply_schema(self) -> None:
        # NOTE: executescript() issues an implicit COMMIT before it runs, so
        # it cannot be nested inside our own transaction -- the COMMIT would
        # close the transaction early and the matching COMMIT would fail with
        # "cannot commit - no transaction is active". DDL runs on its own;
        # only the seed data is transactional.
        self._con.executescript(SCHEMA_SQL)
        with self._transaction():
            self._con.execute(
                "INSERT OR IGNORE INTO schema_version (version, applied_at) "
                "VALUES (?, ?)",
                (SCHEMA_VERSION, utc_now()),
            )
            self._con.executemany(
                "INSERT OR IGNORE INTO metric (metric, value_kind, unit, description) "
                "VALUES (?, ?, ?, ?)",
                SEED_METRICS,
            )
        self._root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Run a block atomically.

        A capture that fails halfway must leave no trace. Partial history is
        worse than absent history, because absent history is obvious.
        """
        self._con.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._con.execute("ROLLBACK")
            raise
        self._con.execute("COMMIT")

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> SnapshotStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- artifacts ---------------------------------------------------------

    def put_artifact(
        self,
        raw: bytes,
        *,
        source: str,
        kind: str,
        original_filename: str | None = None,
        retrieved_at: str | datetime | None = None,
    ) -> str:
        """Persist a raw artifact verbatim and register it. Returns its SHA-256.

        The bytes are written to disk exactly as received and never modified.
        If a parser bug surfaces months later, the original can be re-parsed;
        without it, the affected weeks are unrecoverable, because slate data
        cannot be downloaded after the fact.

        Re-storing identical bytes is idempotent -- the digest is the primary
        key, so a repeated capture of unchanged data does not duplicate it.
        """
        digest = hashlib.sha256(raw).hexdigest()
        retrieved = normalize_timestamp(retrieved_at) if retrieved_at else utc_now()

        target = self._root / digest[:2] / f"{digest}.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(raw)

        with self._transaction():
            self._con.execute(
                "INSERT OR IGNORE INTO artifact "
                "(sha256, source, kind, original_filename, stored_path, "
                " byte_size, retrieved_at) VALUES (?,?,?,?,?,?,?)",
                (
                    digest,
                    source,
                    kind,
                    original_filename,
                    str(target.relative_to(self._root.parent)),
                    len(raw),
                    retrieved,
                ),
            )
        return digest

    def artifact_bytes(self, sha256: str) -> bytes:
        """Read a stored artifact back, verifying its digest still matches.

        Silent bit-rot in the raw zone would undermine every provenance claim
        the store makes, so the check is unconditional rather than optional.
        """
        row = self._con.execute(
            "SELECT stored_path FROM artifact WHERE sha256 = ?", (sha256,)
        ).fetchone()
        if row is None:
            raise StoreError(f"no artifact registered with digest {sha256}")

        data = (self._root.parent / row["stored_path"]).read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != sha256:
            raise StoreError(
                f"artifact digest mismatch: registered {sha256}, found {actual}"
            )
        return data

    # -- observations ------------------------------------------------------

    def record(
        self,
        *,
        subject_type: str,
        source: str,
        source_subject_id: str,
        metric: str,
        value: float | int | str,
        effective_at: str | datetime,
        captured_at: str | datetime | None = None,
        artifact_sha256: str,
        on_duplicate: Literal["error", "ignore"] = "error",
    ) -> None:
        """Record a single observation.

        ``captured_at`` defaults to now, which is correct for a live capture.
        Pass it explicitly when back-filling from an artifact retrieved
        earlier, otherwise the store will claim we knew things sooner than we
        did -- the precise error bitemporality exists to prevent.

        Duplicates raise by default. Re-running a partially failed capture is
        a legitimate reason to pass ``on_duplicate="ignore"``.
        """
        self.record_many(
            [
                dict(
                    subject_type=subject_type,
                    source=source,
                    source_subject_id=source_subject_id,
                    metric=metric,
                    value=value,
                    effective_at=effective_at,
                    captured_at=captured_at,
                )
            ],
            artifact_sha256=artifact_sha256,
            on_duplicate=on_duplicate,
        )

    def record_many(
        self,
        observations: Iterable[dict],
        *,
        artifact_sha256: str,
        on_duplicate: Literal["error", "ignore"] = "error",
    ) -> int:
        """Record many observations atomically. Returns the number inserted.

        All or nothing: one bad row aborts the whole batch. A capture that
        half-succeeded would leave the store claiming a complete snapshot it
        does not have.
        """
        verb = "INSERT OR IGNORE INTO" if on_duplicate == "ignore" else "INSERT INTO"
        known = self._known_metrics()
        rows = []

        for obs in observations:
            metric = obs["metric"]
            if metric not in known:
                raise UnknownMetric(
                    f"unregistered metric {metric!r}. Register it in "
                    f"SEED_METRICS or the metric table before recording it."
                )
            value = obs["value"]
            is_num = known[metric] == "num"
            if is_num and isinstance(value, str):
                raise StoreError(
                    f"metric {metric!r} is numeric but got a string: {value!r}"
                )
            if not is_num and not isinstance(value, str):
                raise StoreError(
                    f"metric {metric!r} is textual but got {type(value).__name__}"
                )

            captured = obs.get("captured_at")
            rows.append(
                (
                    obs["subject_type"],
                    obs["source"],
                    str(obs["source_subject_id"]),
                    metric,
                    float(value) if is_num else None,
                    None if is_num else value,
                    normalize_timestamp(obs["effective_at"]),
                    normalize_timestamp(captured) if captured else utc_now(),
                    artifact_sha256,
                )
            )

        with self._transaction():
            cur = self._con.executemany(
                f"{verb} observation "
                "(subject_type, source, source_subject_id, metric, value_num, "
                " value_text, effective_at, captured_at, artifact_sha256) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )
            return cur.rowcount

    def _known_metrics(self) -> dict[str, str]:
        return {
            r["metric"]: r["value_kind"]
            for r in self._con.execute("SELECT metric, value_kind FROM metric")
        }

    # -- point-in-time reconstruction --------------------------------------

    #: The query the whole design exists to make correct.
    #:
    #: Both timestamps must fall at or before the cutoff. Filtering on
    #: effective_at alone would admit records that describe the past but did
    #: not reach us until after the decision was made -- future information
    #: laundered through a plausible-looking timestamp.
    _AS_OF_SQL = """
        SELECT subject_type, source, source_subject_id, metric,
               value_num, value_text, effective_at, captured_at, artifact_sha256
        FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY source, source_subject_id, metric
                ORDER BY effective_at DESC, captured_at DESC, obs_id DESC
            ) AS rn
            FROM observation
            WHERE effective_at <= :cutoff
              AND captured_at  <= :cutoff
              AND (:source       IS NULL OR source       = :source)
              AND (:metric       IS NULL OR metric       = :metric)
              AND (:subject_type IS NULL OR subject_type = :subject_type)
        )
        WHERE rn = 1
        ORDER BY source, source_subject_id, metric
    """

    def as_of(
        self,
        cutoff: str | datetime,
        *,
        source: str | None = None,
        metric: str | None = None,
        subject_type: str | None = None,
    ) -> list[Observation]:
        """Reconstruct the information state knowable at ``cutoff``.

        Returns the most recent observation per (source, subject, metric)
        that was *both* effective and captured at or before the cutoff.

        The ``obs_id`` tiebreak in the ordering makes the result deterministic
        when two observations share both timestamps, so a backtest run twice
        returns the same answer.
        """
        cursor = self._con.execute(
            self._AS_OF_SQL,
            {
                "cutoff": normalize_timestamp(cutoff),
                "source": source,
                "metric": metric,
                "subject_type": subject_type,
            },
        )
        return [
            Observation(
                subject_type=r["subject_type"],
                source=r["source"],
                source_subject_id=r["source_subject_id"],
                metric=r["metric"],
                value=r["value_num"] if r["value_num"] is not None else r["value_text"],
                effective_at=r["effective_at"],
                captured_at=r["captured_at"],
                artifact_sha256=r["artifact_sha256"],
            )
            for r in cursor
        ]

    # -- introspection -----------------------------------------------------

    def observation_count(self) -> int:
        return self._con.execute("SELECT COUNT(*) FROM observation").fetchone()[0]

    def artifact_count(self) -> int:
        return self._con.execute("SELECT COUNT(*) FROM artifact").fetchone()[0]

    def schema_version(self) -> int:
        return self._con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
