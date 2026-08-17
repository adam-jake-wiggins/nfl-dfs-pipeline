"""Capture orchestration: read a source, archive the raw bytes, record observations.

The order matters and is deliberate. The raw artifact is stored *first*, so
that if parsing or recording fails we still hold the bytes and can retry
later. Slate data cannot be re-downloaded once the week passes; losing it to
a parser bug would be unrecoverable, while an unparsed artifact is merely
inconvenient.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from dfs_pipeline.adapters.base import SalarySource, SlatePlayer
from dfs_pipeline.store import SnapshotStore, normalize_timestamp, utc_now

__all__ = [
    "CaptureResult",
    "OddsCaptureResult",
    "ResultsCaptureResult",
    "ingest_slate",
    "ingest_odds",
    "ingest_results",
]


@dataclass(frozen=True, slots=True)
class CaptureResult:
    """What a capture actually did, for the run report."""

    source: str
    artifact_sha256: str
    players: int
    defenses: int
    games: int
    observations: int
    effective_at: str
    captured_at: str

    @property
    def total_entries(self) -> int:
        return self.players + self.defenses


def ingest_slate(
    store: SnapshotStore,
    source: SalarySource,
    *,
    effective_at: str | datetime | None = None,
    captured_at: str | datetime | None = None,
    original_filename: str | None = None,
    on_duplicate: str = "error",
) -> CaptureResult:
    """Archive a slate's raw bytes and record its observations.

    ``effective_at`` -- when the source says the information was current --
    defaults to ``captured_at``. That default is a real modelling decision
    worth stating: a manually downloaded DraftKings CSV carries **no
    self-reported timestamp**, so we genuinely do not know when the salaries
    were set. Claiming to know would be worse than admitting we do not; the
    honest statement is "current as of when we captured it."

    Pass ``effective_at`` explicitly when back-filling a file downloaded
    earlier, otherwise the store will record that we knew things sooner than
    we did -- precisely the error bitemporality exists to prevent.
    """
    captured = normalize_timestamp(captured_at) if captured_at else utc_now()
    effective = normalize_timestamp(effective_at) if effective_at else captured

    # Archive before parsing. An unparsed artifact can be re-parsed; an
    # unarchived slate is gone.
    raw = source.raw_bytes()
    sha = store.put_artifact(
        raw,
        source=source.source_name,
        kind="slate_salaries",
        original_filename=original_filename,
        retrieved_at=captured,
    )

    players = source.loads(raw)
    observations = list(
        _observations_for(players, source.source_name, effective, captured)
    )
    written = store.record_many(
        observations, artifact_sha256=sha, on_duplicate=on_duplicate
    )

    return CaptureResult(
        source=source.source_name,
        artifact_sha256=sha,
        players=sum(1 for p in players if not p.is_defense),
        defenses=sum(1 for p in players if p.is_defense),
        games=len({p.game.key for p in players}),
        observations=written,
        effective_at=effective,
        captured_at=captured,
    )


@dataclass(frozen=True, slots=True)
class OddsCaptureResult:
    """What an odds capture did, for the run report."""

    artifact_sha256: str
    games: int
    bookmakers: int
    team_rows: int
    observations: int
    captured_at: str
    quota_remaining: int | None = None


def ingest_odds(
    store: SnapshotStore,
    source,
    *,
    captured_at: str | datetime | None = None,
    on_duplicate: str = "error",
) -> OddsCaptureResult:
    """Archive an odds response and record its observations.

    Unlike the DraftKings CSV, odds carry their *own* timestamps: each market
    reports the moment the bookmaker last moved that line. Those become
    ``effective_at``, per row, while ``captured_at`` is when we read them.
    There is no single effective time for the whole response -- one book may
    have moved a line minutes ago and another hours ago -- which is precisely
    why the store keeps the two timestamps per observation rather than per
    capture.
    """
    captured = normalize_timestamp(captured_at) if captured_at else utc_now()

    raw = source.raw_bytes()
    sha = store.put_artifact(
        raw,
        source=source.source_name,
        kind="odds_snapshot",
        retrieved_at=captured,
    )

    rows = source.loads(raw)
    observations = list(_odds_observations(rows, captured))
    written = store.record_many(
        observations, artifact_sha256=sha, on_duplicate=on_duplicate
    )

    return OddsCaptureResult(
        artifact_sha256=sha,
        games=len({r.event_id for r in rows}),
        bookmakers=len({r.bookmaker for r in rows}),
        team_rows=len(rows),
        observations=written,
        captured_at=captured,
        quota_remaining=getattr(source, "last_quota_remaining", None),
    )


def _odds_observations(rows, captured: str):
    """Flatten per-team odds into narrow observation rows.

    Each bookmaker becomes its own source. Books disagree, and that
    disagreement is signal that cannot be recovered from a consensus computed
    at capture time and stored alone.
    """
    for row in rows:
        base = dict(
            subject_type="team",
            source=row.source_name,
            source_subject_id=row.subject_id,
            effective_at=row.effective_at,
            captured_at=captured,
        )
        yield {**base, "metric": "odds_team", "value": row.team}
        yield {**base, "metric": "odds_game", "value": row.game_key}
        yield {**base, "metric": "odds_commence_time", "value": row.commence_time}

        if row.spread is not None:
            yield {**base, "metric": "spread", "value": row.spread}
        if row.game_total is not None:
            yield {**base, "metric": "game_total", "value": row.game_total}

        implied = row.implied_team_total
        if implied is not None:
            yield {**base, "metric": "implied_team_total", "value": implied}


def _observations_for(
    players: list[SlatePlayer], source: str, effective: str, captured: str
):
    """Flatten normalized players into narrow observation rows."""
    for p in players:
        base = dict(
            subject_type=p.entity_type,
            source=source,
            source_subject_id=p.source_player_id,
            effective_at=effective,
            captured_at=captured,
        )
        yield {**base, "metric": "dk_salary", "value": float(p.salary)}
        yield {**base, "metric": "dk_player_name", "value": p.name}
        yield {**base, "metric": "dk_position", "value": p.position}
        yield {**base, "metric": "dk_team", "value": p.team}
        yield {**base, "metric": "dk_game", "value": p.game.key}

        if p.avg_points_per_game is not None:
            yield {
                **base,
                "metric": "dk_avg_points",
                "value": float(p.avg_points_per_game),
            }
        if p.roster_positions:
            yield {
                **base,
                "metric": "dk_roster_position",
                "value": "/".join(p.roster_positions),
            }
        if p.status is not None:
            yield {**base, "metric": "dk_status", "value": p.status}
        if p.lock_time_utc is not None:
            yield {**base, "metric": "dk_lock_time", "value": p.lock_time_utc}


@dataclass(frozen=True, slots=True)
class ResultsCaptureResult:
    """What a results capture did, for the run report."""

    artifact_sha256: str
    season: int
    week: int
    players: int
    defenses: int
    observations: int
    captured_at: str

    @property
    def total_entities(self) -> int:
        return self.players + self.defenses


def ingest_results(
    store: SnapshotStore,
    results: list,
    raw: bytes,
    *,
    season: int,
    week: int,
    captured_at: str | datetime | None = None,
    effective_at: str | datetime | None = None,
    on_duplicate: str = "error",
) -> ResultsCaptureResult:
    """Record realized DraftKings points for one completed week.

    ``effective_at`` defaults to ``captured_at``. nflverse does not stamp its
    tables with a computation time, and the honest statement is "this is what
    the source said when we read it" -- particularly because nflverse revises
    prior weeks as official corrections land. Recording when *we* read it is
    what makes those revisions visible later as separate observations rather
    than as an overwrite.
    """
    captured = normalize_timestamp(captured_at) if captured_at else utc_now()
    effective = normalize_timestamp(effective_at) if effective_at else captured

    sha = store.put_artifact(
        raw,
        source="NFLVERSE",
        kind="weekly_results",
        original_filename=f"nflverse_{season}_wk{week}.json",
        retrieved_at=captured,
    )

    observations = list(_result_observations(results, effective, captured))
    written = store.record_many(
        observations, artifact_sha256=sha, on_duplicate=on_duplicate
    )

    return ResultsCaptureResult(
        artifact_sha256=sha,
        season=season,
        week=week,
        players=sum(1 for r in results if r.entity_type == "player"),
        defenses=sum(1 for r in results if r.entity_type == "dst"),
        observations=written,
        captured_at=captured,
    )


def _result_observations(results, effective: str, captured: str):
    """Flatten scored results into narrow observation rows.

    Keyed on the nflverse id (or team abbreviation for a defense), NOT on the
    DraftKings player id: results come from a different source with its own
    identifiers, and forcing a join at capture time would drop any player the
    crosswalk cannot yet resolve.
    """
    for r in results:
        base = dict(
            subject_type=r.entity_type,
            source="NFLVERSE",
            source_subject_id=r.nflverse_id,
            effective_at=effective,
            captured_at=captured,
        )
        yield {**base, "metric": "actual_dk_points", "value": float(r.dk_points)}
        yield {**base, "metric": "nflverse_name", "value": r.name}
        yield {**base, "metric": "nflverse_team", "value": r.team}
        yield {**base, "metric": "nflverse_position", "value": r.position}
