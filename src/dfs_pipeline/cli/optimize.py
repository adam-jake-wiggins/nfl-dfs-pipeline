"""``dfs-optimize`` -- raw inputs to an upload-ready lineup file, in one run.

Satisfies the acceptance criterion that one command take raw inputs through to
lineups, writing a self-contained run directory: the lineup file, a match
report, a validation report, and run metadata carrying the resolved config,
every input's SHA-256, and timestamps.

The pipeline, in order::

    slate  ->  projections  ->  match report  ->  pool filter
           ->  MILP solve   ->  slot assignment  ->  upload CSV

Each stage reports what it did. The prototype's defining failure was a silent
one -- an unmatched name degrading to a season average while the run announced
success -- so every stage that can quietly lose a player says how many it lost
and which.

Validation is not optional
==========================
Every lineup is re-checked *after* the solver returns: roster shape, salary
cap, distinct games, no duplicate player, and it must survive slot assignment.
The solver already enforces all of that, which is exactly why the check is
worth having -- it catches the case where our model and our understanding of
the rules have drifted apart. A validation report that always passes costs
nothing; one that ever fails has earned its keep.

Exit codes
----------
0   lineups written
1   runtime failure
2   usage error
3   input data rejected, or no lineups could be built
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

from dfs_pipeline import __version__
from dfs_pipeline.adapters import (
    DraftKingsApiAdapter,
    DraftKingsApiError,
    DraftKingsCsvAdapter,
    ProjectionsCsvAdapter,
    SlateSchemaError,
)
from dfs_pipeline.config import ConfigError, load_config
from dfs_pipeline.contest import (
    MIN_DISTINCT_GAMES,
    ROSTER_SIZE,
    SALARY_CAP,
    is_legal_roster_shape,
)
from dfs_pipeline.lineup import UnassignableLineup, assign_slots
from dfs_pipeline.names import normalize_name
from dfs_pipeline.optimizer import Settings, optimize
from dfs_pipeline.pool import filter_pool
from dfs_pipeline.runs import RunDirectory
from dfs_pipeline.upload import UploadError, write_upload_csv

__all__ = ["main", "build_parser", "validate_lineups"]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_DATA = 3

log = logging.getLogger("dfs_pipeline.optimize")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dfs-optimize",
        description="Build DraftKings lineups from a slate and projections.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    src = parser.add_argument_group("inputs")
    src.add_argument("--salaries", metavar="CSV", help="DraftKings salary export")
    src.add_argument("--slate-api", action="store_true",
                     help="Fetch the slate from DraftKings instead of a CSV")
    src.add_argument("--draft-group", type=int, metavar="ID")
    src.add_argument("--projections", metavar="CSV", required=True,
                     help="Projection export. Required: without projections the "
                          "optimizer would be maximising nothing.")
    src.add_argument("--projections-source", metavar="NAME", default="DFF")

    shape = parser.add_argument_group("lineup construction")
    shape.add_argument("--lineups", type=int, default=1)
    shape.add_argument("--stack", type=int, default=0,
                       help="Pass catchers required from the QB's own team")
    shape.add_argument("--bringback", type=int, default=0,
                       help="Players required from the team opposing the QB")
    shape.add_argument("--max-exposure", type=float, default=1.0)
    shape.add_argument("--min-salary", type=int, default=0)
    shape.add_argument("--min-unique", type=int, default=1)
    shape.add_argument("--exclude-status", metavar="LIST", default=None,
                       help="Statuses to exclude (default: OUT,IR,PUP,SUSPENDED). "
                            "Pass an empty string to disable.")
    shape.add_argument("--ban", action="append", default=[], metavar="NAME")

    where = parser.add_argument_group("locations")
    where.add_argument("--config", metavar="TOML")
    where.add_argument("--runs", dest="runs_directory", metavar="DIR")
    where.add_argument("--output", metavar="CSV",
                       help="Also write the lineup file here, outside the run "
                            "directory")

    how = parser.add_argument_group("behaviour")
    how.add_argument("--time-limit", type=float, metavar="SECONDS")
    how.add_argument("-v", "--verbose", action="count", default=0)
    return parser


def validate_lineups(lineups) -> list[dict]:
    """Re-check every lineup against the contest rules after the solve.

    Returns a list of problems, empty when everything is legal.
    """
    problems = []
    for index, lineup in enumerate(lineups, start=1):
        issues = []

        if len(lineup) != ROSTER_SIZE:
            issues.append(f"{len(lineup)} players, expected {ROSTER_SIZE}")

        ids = [p.source_player_id for p in lineup]
        if len(set(ids)) != len(ids):
            issues.append("a player appears more than once")

        counts = dict(Counter(p.position for p in lineup))
        if not is_legal_roster_shape(counts):
            issues.append(f"illegal roster shape {counts}")

        salary = sum(p.salary for p in lineup)
        if salary > SALARY_CAP:
            issues.append(f"salary {salary} exceeds the {SALARY_CAP} cap")

        games = {p.game.key for p in lineup}
        if len(games) < MIN_DISTINCT_GAMES:
            issues.append(f"only {len(games)} distinct game(s)")

        try:
            assign_slots(lineup)
        except UnassignableLineup as exc:
            issues.append(f"cannot fill roster slots: {exc}")

        if issues:
            problems.append({"lineup": index, "issues": issues})
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if bool(args.salaries) == bool(args.slate_api):
        parser.error("supply exactly one of --salaries or --slate-api.")

    try:
        config = load_config(
            args.config, overrides={"runs_directory": args.runs_directory}
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    console = {0: logging.WARNING, 1: logging.INFO}.get(args.verbose, logging.DEBUG)

    try:
        with RunDirectory(
            config.runs_directory, command="optimize", console_level=console
        ) as run:
            return _run(args, config, run)
    except SlateSchemaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("The input was rejected; no lineups were written.", file=sys.stderr)
        return EXIT_DATA
    except (UploadError, UnassignableLineup) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_DATA
    except DraftKingsApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _run(args, config, run) -> int:
    run.record.config = {
        "source_file": str(config.source_file) if config.source_file else None,
        "runs_directory": str(config.runs_directory),
        "lineups": args.lineups,
        "stack": args.stack,
        "bringback": args.bringback,
        "max_exposure": args.max_exposure,
        "min_salary": args.min_salary,
        "min_unique": args.min_unique,
        "exclude_status": args.exclude_status,
        "banned": args.ban,
    }

    # -- slate ------------------------------------------------------------
    if args.salaries:
        slate_source = DraftKingsCsvAdapter(args.salaries)
        slate_label = Path(args.salaries).name
    else:
        slate_source = DraftKingsApiAdapter(draft_group_id=args.draft_group)
        slate_label = "draftkings draftables"

    raw_slate = slate_source.raw_bytes()
    slate = slate_source.loads(raw_slate)
    run.record_input(slate_label, sha256=_digest(raw_slate),
                     byte_size=len(raw_slate), kind="slate")
    print(f"Slate: {len(slate)} entries across "
          f"{len({p.game.key for p in slate})} games ({slate_label}).")

    # -- projections ------------------------------------------------------
    projections_source = ProjectionsCsvAdapter(
        args.projections, source_name=args.projections_source
    )
    raw_projections = projections_source.raw_bytes()
    projections = projections_source.loads(raw_projections)
    run.record_input(args.projections, sha256=_digest(raw_projections),
                     byte_size=len(raw_projections), kind="projections")

    by_name = {row.normalized_name: row.projection for row in projections}
    matched = {
        p.source_player_id: by_name[normalize_name(p.name)]
        for p in slate if normalize_name(p.name) in by_name
    }

    unmatched = [p for p in slate if p.source_player_id not in matched]
    expensive = sorted(
        (p for p in unmatched if p.salary >= 5000), key=lambda p: -p.salary
    )
    match_report = {
        "projection_source": projections_source.source_name,
        "slate_entries": len(slate),
        "projection_rows": len(projections),
        "matched": len(matched),
        "match_rate": round(len(matched) / max(1, len(slate)), 4),
        "unmatched_over_5000": [
            {"name": p.name, "position": p.position, "salary": p.salary}
            for p in expensive[:25]
        ],
    }
    (run.path / "match_report.json").write_text(
        json.dumps(match_report, indent=2) + "\n"
    )
    run.results["match"] = match_report

    print(f"Projections ({projections_source.source_name}): "
          f"{len(matched)}/{len(slate)} matched ({match_report['match_rate']:.1%}).")
    if expensive:
        print(f"  WARNING: {len(expensive)} entry(s) at $5,000+ have no projection:")
        for p in expensive[:5]:
            print(f"    ${p.salary:>6,}  {p.name} ({p.position})")

    # A player without a projection cannot be optimized toward; keeping them
    # at zero would let the solver treat them as strictly bad rather than
    # unknown, which is a different and wrong claim.
    projected = [p for p in slate if p.source_player_id in matched]
    if not projected:
        print("error: no slate entry matched a projection.", file=sys.stderr)
        return EXIT_DATA

    # -- pool -------------------------------------------------------------
    pool, pool_report = filter_pool(
        projected,
        exclude_statuses=args.exclude_status,
        exclude_names=args.ban,
    )
    print(pool_report.render())
    run.results["pool"] = {
        "considered": pool_report.considered,
        "kept": pool_report.kept,
        "status_data_present": pool_report.status_data_present,
        "excluded_by_status": {
            k: len(v) for k, v in pool_report.excluded_by_status.items()
        },
    }

    # -- solve ------------------------------------------------------------
    settings = Settings(
        lineups=args.lineups, stack=args.stack, bringback=args.bringback,
        max_exposure=args.max_exposure, min_salary=args.min_salary,
        min_unique=args.min_unique, time_limit=args.time_limit,
    )
    lineups, report = optimize(
        pool, settings, projection_of=lambda p: matched[p.source_player_id]
    )
    print(report.render())
    run.results["optimizer"] = {
        "requested": report.requested,
        "produced": report.produced,
        "mode": report.mode,
        "seconds": round(report.seconds, 3),
        "binding_constraint": report.binding_constraint,
    }

    if not lineups:
        print("error: no lineups could be built.", file=sys.stderr)
        return EXIT_DATA

    # -- validate ---------------------------------------------------------
    problems = validate_lineups(lineups)
    (run.path / "validation_report.json").write_text(
        json.dumps({"lineups": len(lineups), "problems": problems}, indent=2) + "\n"
    )
    run.results["validation"] = {"lineups": len(lineups), "problems": len(problems)}

    if problems:
        print(f"error: {len(problems)} lineup(s) failed validation after the solve.",
              file=sys.stderr)
        for problem in problems[:5]:
            print(f"  lineup {problem['lineup']}: {'; '.join(problem['issues'])}",
                  file=sys.stderr)
        return EXIT_DATA
    print(f"Validation: {len(lineups)} lineup(s), 0 problems.")

    # -- write ------------------------------------------------------------
    rows = [
        [(p.name, p.source_player_id) for p in assign_slots(lineup)]
        for lineup in lineups
    ]
    target = run.path / "lineups.csv"
    write_upload_csv(rows, target)
    if args.output:
        write_upload_csv(rows, args.output)

    run.results["output"] = {
        "lineups": len(rows),
        "path": str(target),
        "also_written_to": args.output,
    }
    print(f"Wrote {len(rows)} lineup(s) to {target}")
    if args.output:
        print(f"  and to {args.output}")
    print(f"  run: {run.path}")
    return EXIT_OK


def _digest(raw: bytes) -> str:
    import hashlib

    return hashlib.sha256(raw).hexdigest()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
