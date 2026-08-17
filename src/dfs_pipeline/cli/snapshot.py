"""``dfs-snapshot`` -- capture one weekly bundle of point-in-time slate data.

Currently implemented: the DraftKings salary CSV path. Projections and odds
capture will attach to the same command as additional flags, so a full weekly
bundle stays one invocation.

Every run writes a self-contained directory under ``runs/`` holding the
resolved config, the SHA-256 of every input, timestamps, and the outcome --
written whether the run succeeds or fails.

Exit codes
----------
0   capture succeeded
1   runtime failure (store unavailable, permissions, unexpected error)
2   usage error (argparse)
3   input data rejected -- schema or validation failure
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

from dfs_pipeline import __version__
from dfs_pipeline.adapters.odds_api import API_BASE
from dfs_pipeline.adapters import (
    DraftKingsApiAdapter,
    DraftKingsApiError,
    DraftKingsCsvAdapter,
    FantasyProsCsvAdapter,
    ProjectionsCsvAdapter,
    OddsApiAdapter,
    OddsApiError,
    SlateSchemaError,
)
from dfs_pipeline.capture import (
    ingest_odds,
    ingest_projections,
    ingest_results,
    ingest_slate,
)
from dfs_pipeline.names import normalize_name
from dfs_pipeline.secrets import MissingSecret, read_odds_api_key
from dfs_pipeline.config import Config, ConfigError, load_config
from dfs_pipeline.runs import RunDirectory
from dfs_pipeline.store import SnapshotStore, StoreError

__all__ = ["main", "build_parser", "EXIT_OK", "EXIT_ERROR", "EXIT_DATA"]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_DATA = 3

log = logging.getLogger("dfs_pipeline.snapshot")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dfs-snapshot",
        description="Capture point-in-time DFS slate data into the snapshot store.",
        epilog=(
            "Timestamps: --captured-at defaults to now. --effective-at defaults "
            "to --captured-at, because a manually downloaded DraftKings CSV "
            "carries no timestamp of its own. Pass --effective-at explicitly "
            "only when back-filling a file you obtained earlier."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    src = parser.add_argument_group("sources")
    src.add_argument(
        "--salaries",
        metavar="CSV",
        help="DraftKings salary export (DKSalaries.csv) to ingest",
    )
    src.add_argument(
        "--slate-api",
        action="store_true",
        help="Capture the slate from DraftKings' draftables endpoint instead "
             "of a CSV. Read-only and unauthenticated; the manual --salaries "
             "import remains a fully supported equal.",
    )
    src.add_argument(
        "--draft-group",
        type=int,
        metavar="ID",
        help="Draft group to capture with --slate-api. Omit to auto-select the "
             "largest non-simulated multi-game slate.",
    )
    src.add_argument(
        "--projections",
        metavar="CSV",
        help="Projection export to ingest (Daily Fantasy Fuel, Stokastic, etc.)",
    )
    src.add_argument(
        "--projections-source",
        metavar="NAME",
        default="DFF",
        help="Which vendor produced --projections (default: DFF). Each vendor "
             "is stored as its own source so their series never merge.",
    )
    src.add_argument(
        "--fantasypros",
        metavar="CSV",
        action="append",
        help="FantasyPros per-position export. Repeatable, once per position. "
             "Format is POSITION=PATH, e.g. --fantasypros QB=fp_qb.csv. These "
             "are season per-game averages, stored under a distinct metric.",
    )
    src.add_argument(
        "--odds",
        action="store_true",
        help="Capture betting spreads and totals from The Odds API. Requires "
             "ODDS_API_KEY in the environment or .env.",
    )
    src.add_argument(
        "--odds-days",
        type=int,
        default=8,
        metavar="N",
        help="Only capture games kicking off within N days (default: 8). "
             "Without a window the API returns the entire season.",
    )
    src.add_argument(
        "--min-quota",
        type=int,
        default=25,
        metavar="N",
        help="Refuse an odds call that would leave fewer than N credits "
             "(default: 25). Guards against a scheduled job exhausting the "
             "monthly budget before a live slate.",
    )
    src.add_argument(
        "--results",
        action="store_true",
        help="Capture realized DraftKings points for a completed week from "
             "nflverse. Requires --season and --week.",
    )
    src.add_argument("--season", type=int, metavar="YYYY",
                     help="Season for --results, e.g. 2025")
    src.add_argument("--week", type=int, metavar="N",
                     help="Week for --results")
    src.add_argument(
        "--quota",
        action="store_true",
        help="Report remaining Odds API credits and exit. Costs nothing.",
    )

    when = parser.add_argument_group("timestamps")
    when.add_argument(
        "--captured-at",
        metavar="ISO8601",
        help="When this data was obtained. Defaults to now.",
    )
    when.add_argument(
        "--effective-at",
        metavar="ISO8601",
        help="When the source says the data was current. Defaults to --captured-at.",
    )

    where = parser.add_argument_group("locations")
    where.add_argument("--config", metavar="TOML", help="Config file (default: ./dfs.toml)")
    where.add_argument("--store", dest="store_path", metavar="PATH", help="Snapshot store path")
    where.add_argument("--runs", dest="runs_directory", metavar="DIR", help="Run directory root")

    how = parser.add_argument_group("behaviour")
    how.add_argument(
        "--on-duplicate",
        dest="on_duplicate",
        choices=("error", "ignore"),
        help="What to do if these observations were already captured",
    )
    how.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and validate the inputs, then stop without writing to the store",
    )
    how.add_argument(
        "--show-config",
        action="store_true",
        help="Print the resolved configuration and where each value came from, then exit",
    )
    how.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Show progress on the console (-vv for debug). The run log always "
             "records everything regardless.",
    )
    return parser


def _console_level(verbosity: int) -> int:
    return {0: logging.WARNING, 1: logging.INFO}.get(verbosity, logging.DEBUG)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(
            args.config,
            overrides={
                "store_path": args.store_path,
                "runs_directory": args.runs_directory,
                "on_duplicate": args.on_duplicate,
            },
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.show_config:
        print(config.describe())
        return EXIT_OK

    if args.quota:
        return _report_quota(args)

    if args.results and (args.season is None or args.week is None):
        parser.error("--results requires both --season and --week.")

    if args.salaries and args.slate_api:
        parser.error(
            "--salaries and --slate-api both capture a slate; choose one. "
            "They produce identical records, so capturing both would only "
            "duplicate the observations."
        )

    if not any((args.salaries, args.slate_api, args.odds, args.results,
                args.projections, args.fantasypros)):
        parser.error(
            "nothing to capture. Supply --salaries, --projections, "
            "--fantasypros, --odds, and/or --results (see --help)."
        )

    return _run(args, config)


def _run(args: argparse.Namespace, config: Config) -> int:
    console_level = _console_level(args.verbose)

    if args.dry_run:
        # A dry run touches neither the store nor the run directory: it exists
        # to answer "would this input be accepted?" without side effects.
        return _dry_run(args, console_level)

    try:
        with RunDirectory(
            config.runs_directory, command="snapshot", console_level=console_level
        ) as run:
            run.record.config = {
                "source_file": str(config.source_file) if config.source_file else None,
                "store_path": str(config.store_path),
                "runs_directory": str(config.runs_directory),
                "on_duplicate": config.on_duplicate,
                "origins": config.origins,
            }
            log.info("store: %s", config.store_path)

            store = SnapshotStore.open(config.store_path)
            try:
                if args.salaries:
                    _capture_salaries(args, config, store, run)
                if args.slate_api:
                    _capture_slate_api(args, config, store, run)
                if args.projections:
                    _capture_projections(args, config, store, run)
                if args.fantasypros:
                    _capture_fantasypros(args, config, store, run)
                if args.odds:
                    _capture_odds(args, config, store, run)
                if args.results:
                    _capture_results(args, config, store, run)
            finally:
                store.close()
            return EXIT_OK

    except SlateSchemaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("The input was rejected; nothing was recorded.", file=sys.stderr)
        return EXIT_DATA
    except sqlite3.IntegrityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if "UNIQUE" in str(exc):
            print(
                "These observations were already captured with the same "
                "timestamps. Re-run with --on-duplicate ignore if that is "
                "expected.",
                file=sys.stderr,
            )
        return EXIT_DATA
    except MissingSecret as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except DraftKingsApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OddsApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_DATA
    except (StoreError, OSError, sqlite3.Error) as exc:
        # sqlite3.Error is caught explicitly because it descends from neither
        # StoreError nor OSError: an unopenable database file raises
        # sqlite3.OperationalError, which would otherwise escape as a raw
        # traceback. "Never a traceback, never silence" applies to the
        # environment failing just as much as to bad input.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _capture_salaries(args, config: Config, store, run) -> None:
    adapter = DraftKingsCsvAdapter(args.salaries)
    result = ingest_slate(
        store,
        adapter,
        effective_at=args.effective_at,
        captured_at=args.captured_at,
        original_filename=Path(args.salaries).name,
        on_duplicate=config.on_duplicate,
    )
    run.record_input(
        args.salaries,
        sha256=result.artifact_sha256,
        byte_size=len(adapter.raw_bytes()),
        kind="dk_salaries_csv",
    )
    run.results["slate"] = {
        "source": result.source,
        "players": result.players,
        "defenses": result.defenses,
        "entries": result.total_entries,
        "games": result.games,
        "observations": result.observations,
        "effective_at": result.effective_at,
        "captured_at": result.captured_at,
    }

    print(f"Slate: captured {result.total_entries} entries "
          f"({result.players} players, {result.defenses} defenses) "
          f"across {result.games} games.")
    print(f"  observations : {result.observations:,}")
    print(f"  effective_at : {result.effective_at}")
    print(f"  captured_at  : {result.captured_at}")
    print(f"  artifact     : {result.artifact_sha256[:16]}...")


def _capture_slate_api(args, config: Config, store, run) -> None:
    adapter = DraftKingsApiAdapter(draft_group_id=args.draft_group)
    result = ingest_slate(
        store, adapter,
        effective_at=args.effective_at,
        captured_at=args.captured_at,
        on_duplicate=config.on_duplicate,
    )
    run.record.inputs.append({
        "kind": "dk_draftables_api",
        "path": f"draftgroups/v1/draftgroups/{adapter.draft_group_id}/draftables",
        "filename": None,
        "sha256": result.artifact_sha256,
        "byte_size": None,
    })
    run.results["slate"] = {
        "source": result.source,
        "draft_group_id": adapter.draft_group_id,
        "players": result.players,
        "defenses": result.defenses,
        "entries": result.total_entries,
        "games": result.games,
        "observations": result.observations,
        "effective_at": result.effective_at,
        "captured_at": result.captured_at,
    }
    print(f"Slate (API): captured {result.total_entries} entries "
          f"({result.players} players, {result.defenses} defenses) "
          f"across {result.games} games.")
    print(f"  draft group  : {adapter.draft_group_id}")
    print(f"  observations : {result.observations:,}")
    print(f"  artifact     : {result.artifact_sha256[:16]}...")


def _capture_projections(args, config: Config, store, run) -> None:
    adapter = ProjectionsCsvAdapter(
        args.projections, source_name=args.projections_source
    )
    rows = adapter.load()
    result = ingest_projections(
        store, adapter,
        effective_at=args.effective_at,
        captured_at=args.captured_at,
        original_filename=Path(args.projections).name,
        on_duplicate=config.on_duplicate,
    )
    run.record_input(
        args.projections,
        sha256=result.artifact_sha256,
        byte_size=len(adapter.raw_bytes()),
        kind="projections_csv",
    )
    report = _match_report(args, rows)
    run.results["projections"] = {
        "source": result.source,
        "rows": result.rows,
        "with_ownership": result.with_ownership,
        "observations": result.observations,
        "effective_at": result.effective_at,
        "captured_at": result.captured_at,
        **({"match": report} if report else {}),
    }

    print(f"Projections ({result.source}): {result.rows} rows, "
          f"{result.with_ownership} with ownership.")
    print(f"  observations : {result.observations:,}")
    print(f"  effective_at : {result.effective_at}")
    print(f"  artifact     : {result.artifact_sha256[:16]}...")
    if report:
        _print_match_report(report)


def _capture_fantasypros(args, config: Config, store, run) -> None:
    total_rows = total_obs = 0
    for spec in args.fantasypros:
        position, _, path = spec.partition("=")
        if not path:
            raise SlateSchemaError(
                spec, "expected POSITION=PATH, e.g. QB=fp_qb.csv"
            )
        adapter = FantasyProsCsvAdapter(path, position=position)
        result = ingest_projections(
            store, adapter,
            effective_at=args.effective_at,
            captured_at=args.captured_at,
            original_filename=Path(path).name,
            on_duplicate=config.on_duplicate,
        )
        run.record_input(
            path, sha256=result.artifact_sha256,
            byte_size=len(adapter.raw_bytes()),
            kind=f"fantasypros_{position.lower()}_csv",
        )
        total_rows += result.rows
        total_obs += result.observations
        print(f"FantasyPros {position.upper():<4}: {result.rows} rows "
              f"({result.observations:,} observations)")

    run.results["fantasypros"] = {
        "rows": total_rows,
        "observations": total_obs,
        "metric": FantasyProsCsvAdapter.metric_name,
        "files": len(args.fantasypros),
    }
    print(f"  stored as    : {FantasyProsCsvAdapter.metric_name}")
    print(f"  NOTE         : season per-game averages, not weekly slate "
          f"projections.")


def _match_report(args, projection_rows) -> dict | None:
    """Compare projection names against the slate, if a slate was supplied.

    The prototype's worst defect was a silent name-match failure that degraded
    projections invisibly. A match rate that is never reported is a match rate
    nobody checks, so this prints on every run where both inputs are present.

    This is a NAME-level check only. Real identity resolution against nflverse
    ids is the crosswalk's job; this is the loud early-warning that something
    has drifted.
    """
    if not args.salaries:
        return None

    slate = DraftKingsCsvAdapter(args.salaries).load()
    slate_players = [p for p in slate if not p.is_defense]
    slate_keys = {normalize_name(p.name): p for p in slate_players}

    projected = {r.normalized_name for r in projection_rows}
    matched = projected & set(slate_keys)
    unmatched_projections = sorted(projected - set(slate_keys))

    # Unmatched slate players matter most above a salary floor: a missing
    # projection for a $3,000 punt is noise, for a $8,000 player it is a hole.
    unmatched_slate = [
        p for key, p in slate_keys.items() if key not in projected
    ]
    expensive_gaps = sorted(
        (p for p in unmatched_slate if p.salary >= 5000),
        key=lambda p: -p.salary,
    )

    return {
        "slate_players": len(slate_players),
        "projection_rows": len(projection_rows),
        "matched": len(matched),
        "match_rate": round(len(matched) / max(1, len(slate_players)), 4),
        "unmatched_projections": unmatched_projections[:25],
        "unmatched_slate_over_5000": [
            {"name": p.name, "salary": p.salary, "position": p.position}
            for p in expensive_gaps[:25]
        ],
    }


def _print_match_report(report: dict) -> None:
    rate = report["match_rate"]
    print(f"  match rate   : {rate:.1%} "
          f"({report['matched']}/{report['slate_players']} slate players)")
    gaps = report["unmatched_slate_over_5000"]
    if gaps:
        print(f"  WARNING: {len(gaps)} slate player(s) at $5,000+ have no projection:")
        for g in gaps[:8]:
            print(f"    ${g['salary']:>6,}  {g['name']} ({g['position']})")
    orphans = report["unmatched_projections"]
    if orphans:
        print(f"  {len(orphans)} projection name(s) matched no slate player, e.g.:")
        for o in orphans[:5]:
            print(f"    {o}")


def _capture_odds(args, config: Config, store, run) -> None:
    adapter = OddsApiAdapter(
        read_odds_api_key(),
        days_ahead=args.odds_days,
        min_quota=args.min_quota,
    )
    result = ingest_odds(
        store,
        adapter,
        captured_at=args.captured_at,
        on_duplicate=config.on_duplicate,
    )
    run.record.inputs.append(
        {
            "kind": "odds_api_json",
            "path": f"{API_BASE}/sports/americanfootball_nfl/odds/",
            "filename": None,
            "sha256": result.artifact_sha256,
            "byte_size": None,
        }
    )
    run.results["odds"] = {
        "games": result.games,
        "bookmakers": result.bookmakers,
        "team_rows": result.team_rows,
        "observations": result.observations,
        "captured_at": result.captured_at,
        "quota_remaining": result.quota_remaining,
        "window_days": args.odds_days,
    }

    print(f"Odds: captured {result.games} games x {result.bookmakers} bookmakers "
          f"({result.team_rows} team rows).")
    print(f"  observations : {result.observations:,}")
    print(f"  captured_at  : {result.captured_at}")
    print(f"  artifact     : {result.artifact_sha256[:16]}...")
    if result.quota_remaining is not None:
        print(f"  quota left   : {result.quota_remaining}")


def _capture_results(args, config: Config, store, run) -> None:
    from dfs_pipeline.results import load_and_score_week

    results, raw = load_and_score_week(args.season, args.week)
    outcome = ingest_results(
        store, results, raw,
        season=args.season, week=args.week,
        captured_at=args.captured_at,
        effective_at=args.effective_at,
        on_duplicate=config.on_duplicate,
    )
    run.record.inputs.append({
        "kind": "nflverse_weekly",
        "path": f"nflverse {args.season} week {args.week}",
        "filename": f"nflverse_{args.season}_wk{args.week}.json",
        "sha256": outcome.artifact_sha256,
        "byte_size": len(raw),
    })
    run.results["results"] = {
        "season": outcome.season,
        "week": outcome.week,
        "players": outcome.players,
        "defenses": outcome.defenses,
        "observations": outcome.observations,
        "captured_at": outcome.captured_at,
    }

    print(f"Results: scored {outcome.total_entities} entities "
          f"({outcome.players} players, {outcome.defenses} defenses) "
          f"for {outcome.season} week {outcome.week}.")
    print(f"  observations : {outcome.observations:,}")
    print(f"  artifact     : {outcome.artifact_sha256[:16]}...")


def _report_quota(args) -> int:
    """Print remaining Odds API credits. Costs nothing -- /sports is free."""
    try:
        adapter = OddsApiAdapter(read_odds_api_key(), min_quota=args.min_quota)
        remaining = adapter.check_quota()
    except (MissingSecret, OddsApiError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"Odds API credits remaining : {remaining}")
    print(f"Cost of one odds capture   : {adapter.credit_cost} "
          f"({len(adapter.regions)} region x {len(adapter.markets)} markets)")
    print(f"Captures affordable        : {max(0, remaining - args.min_quota) // adapter.credit_cost}"
          f"  (down to the --min-quota floor of {args.min_quota})")
    return EXIT_OK


def _dry_run(args, console_level: int) -> int:
    logging.basicConfig(level=console_level, format="%(levelname)s: %(message)s")
    if not args.salaries:
        print("Dry run supports --salaries only; --odds would spend credits.",
              file=sys.stderr)
        return EXIT_USAGE
    salaries = args.salaries
    try:
        players = DraftKingsCsvAdapter(salaries).load()
    except SlateSchemaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_DATA

    games = {p.game.key for p in players}
    flagged = [p for p in players if p.is_flagged]
    print(f"{Path(salaries).name} is valid.")
    print(f"  entries : {len(players)} "
          f"({sum(1 for p in players if not p.is_defense)} players, "
          f"{sum(1 for p in players if p.is_defense)} defenses)")
    print(f"  games   : {len(games)}")
    print(f"  flagged : {len(flagged)} with an injury designation")
    print("\nDry run: nothing was written.")
    return EXIT_OK



if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
