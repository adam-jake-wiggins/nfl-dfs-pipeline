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
    DraftKingsCsvAdapter,
    OddsApiAdapter,
    OddsApiError,
    SlateSchemaError,
)
from dfs_pipeline.capture import ingest_odds, ingest_results, ingest_slate
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

    if not args.salaries and not args.odds and not args.results:
        parser.error(
            "nothing to capture. Supply --salaries CSV, --odds, and/or "
            "--results (see --help)."
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
