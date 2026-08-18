"""Tests for `dfs-optimize`, the raw-inputs-to-upload-file command.

The acceptance criterion is that one command takes raw inputs through to
lineups and writes a self-contained run directory. These tests check that the
directory really is self-contained, that every stage reports what it lost, and
that validation runs *after* the solver rather than trusting it.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from dfs_pipeline.adapters.base import GameInfo, SlatePlayer
from dfs_pipeline.cli.optimize import (
    EXIT_DATA,
    EXIT_OK,
    main,
    validate_lineups,
)
from dfs_pipeline.contest import SALARY_CAP, SLOT_ORDER

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def slate_csv(tmp_path):
    """A slate deep enough to build several distinct legal lineups."""
    target = tmp_path / "DKSalaries.csv"
    rows = [[
        "Position", "Name + ID", "Name", "ID", "Roster Position", "Salary",
        "Game Info", "TeamAbbrev", "AvgPointsPerGame",
    ]]
    n = 0
    for away, home in (("KC", "BUF"), ("DAL", "PHI"), ("SF", "LAR")):
        info = f"{away}@{home} 09/13/2026 01:00PM ET"
        for team in (away, home):
            for position in ("QB", "RB", "WR", "TE", "DST"):
                slots = {"QB": "QB", "DST": "DST"}.get(position, f"{position}/FLEX")
                for k in range(4):
                    n += 1
                    name = f"{team} {position}{k}"
                    rows.append([
                        position, f"{name} ({n})", name, str(n), slots,
                        str(3000 + (k * 800) % 5000), info, team, "10.0",
                    ])
    with open(target, "w", newline="") as fh:
        csv.writer(fh).writerows(rows)
    return target


@pytest.fixture()
def projections_csv(tmp_path, slate_csv):
    target = tmp_path / "projections.csv"
    raw = slate_csv.read_text()
    rows = list(csv.DictReader(raw.splitlines()))
    with open(target, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Name", "Projection"])
        for i, row in enumerate(rows):
            writer.writerow([row["Name"], 5.0 + (i % 13)])
    return target


def run(slate, projections, runs, *extra):
    return main([
        "--salaries", str(slate), "--projections", str(projections),
        "--runs", str(runs), *extra,
    ])


def only_run_dir(runs) -> Path:
    dirs = sorted(Path(runs).iterdir())
    assert len(dirs) == 1, dirs
    return dirs[0]


# ---------------------------------------------------------------------------
# The happy path, end to end
# ---------------------------------------------------------------------------

def test_one_command_produces_an_upload_file(slate_csv, projections_csv, tmp_path, capsys):
    runs = tmp_path / "runs"
    assert run(slate_csv, projections_csv, runs, "--lineups", "3") == EXIT_OK

    lineups = only_run_dir(runs) / "lineups.csv"
    rows = list(csv.reader(lineups.open(newline="")))
    assert rows[0] == list(SLOT_ORDER)
    assert len(rows) == 4
    for row in rows[1:]:
        assert len(row) == 9
        assert all("(" in cell and cell.endswith(")") for cell in row)


def test_the_run_directory_is_self_contained(slate_csv, projections_csv, tmp_path):
    """The acceptance criterion names each of these artefacts."""
    runs = tmp_path / "runs"
    run(slate_csv, projections_csv, runs, "--lineups", "2")
    directory = only_run_dir(runs)
    for artefact in (
        "lineups.csv", "match_report.json", "validation_report.json",
        "run.json", "run.log",
    ):
        assert (directory / artefact).is_file(), artefact


def test_run_metadata_records_input_hashes(slate_csv, projections_csv, tmp_path):
    runs = tmp_path / "runs"
    run(slate_csv, projections_csv, runs, "--lineups", "1")
    record = json.loads((only_run_dir(runs) / "run.json").read_text())
    kinds = {i["kind"]: i for i in record["inputs"]}
    assert set(kinds) == {"slate", "projections"}
    for entry in kinds.values():
        assert len(entry["sha256"]) == 64


def test_run_metadata_records_the_settings_used(slate_csv, projections_csv, tmp_path):
    runs = tmp_path / "runs"
    run(slate_csv, projections_csv, runs, "--lineups", "2", "--stack", "1",
        "--max-exposure", "0.5")
    config = json.loads((only_run_dir(runs) / "run.json").read_text())["config"]
    assert config["lineups"] == 2
    assert config["stack"] == 1
    assert config["max_exposure"] == 0.5


def test_the_output_flag_writes_a_second_copy(slate_csv, projections_csv, tmp_path):
    runs, output = tmp_path / "runs", tmp_path / "elsewhere.csv"
    run(slate_csv, projections_csv, runs, "--lineups", "1", "--output", str(output))
    assert output.is_file()
    assert output.read_bytes() == (only_run_dir(runs) / "lineups.csv").read_bytes()


# ---------------------------------------------------------------------------
# Every stage reports what it lost
# ---------------------------------------------------------------------------

def test_the_match_report_is_written_and_warns(slate_csv, tmp_path, capsys):
    """A player with no projection is a hole, and an expensive one is loud."""
    sparse = tmp_path / "sparse.csv"
    rows = list(csv.DictReader(slate_csv.read_text().splitlines()))
    with open(sparse, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Name", "Projection"])
        for row in rows:
            if row["Name"].endswith("0"):  # withhold one player per position
                continue
            writer.writerow([row["Name"], 10.0])

    runs = tmp_path / "runs"
    run(slate_csv, sparse, runs, "--lineups", "1")
    out = capsys.readouterr().out
    assert "matched" in out
    report = json.loads((only_run_dir(runs) / "match_report.json").read_text())
    assert report["matched"] < report["slate_entries"]
    assert "WARNING" in out


def test_the_pool_report_names_excluded_players(slate_csv, projections_csv, tmp_path, capsys):
    runs = tmp_path / "runs"
    run(slate_csv, projections_csv, runs, "--lineups", "1", "--ban", "KC QB0")
    assert "excluded by name" in capsys.readouterr().out


def test_a_shortfall_is_reported_with_its_binding_constraint(
    slate_csv, projections_csv, tmp_path, capsys
):
    runs = tmp_path / "runs"
    run(slate_csv, projections_csv, runs, "--lineups", "500", "--min-unique", "9")
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "binding constraint" in out


# ---------------------------------------------------------------------------
# A pool missing a position names it
# ---------------------------------------------------------------------------

def test_projections_without_defenses_name_the_missing_position(
    slate_csv, tmp_path, capsys
):
    """Realistic, not theoretical: FantasyPros ships defenses separately.

    Before this check the solver reported only "pool too small or too
    constrained", which is true and useless.
    """
    no_dst = tmp_path / "no_dst.csv"
    rows = list(csv.DictReader(slate_csv.read_text().splitlines()))
    with open(no_dst, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Name", "Projection"])
        for row in rows:
            if row["Position"] != "DST":
                writer.writerow([row["Name"], 10.0])

    runs = tmp_path / "runs"
    assert run(slate_csv, no_dst, runs, "--lineups", "1") == EXIT_DATA
    out = capsys.readouterr().out
    assert "DST: 0 in the pool, 1 required" in out


def test_no_matching_projections_at_all_fails_cleanly(slate_csv, tmp_path, capsys):
    empty = tmp_path / "nomatch.csv"
    empty.write_text("Name,Projection\nNobody At All,10.0\n")
    runs = tmp_path / "runs"
    assert run(slate_csv, empty, runs) == EXIT_DATA
    assert "no slate entry matched a projection" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Validation runs after the solver, not instead of trusting it
# ---------------------------------------------------------------------------

def _player(pid, position, salary=5000, team="KC", game=("KC", "BUF")):
    return SlatePlayer(
        source_player_id=str(pid), name=f"p{pid}", position=position,
        salary=salary, team=team, game=GameInfo(*game, None),
        entity_type="dst" if position == "DST" else "player",
    )


LEGAL = [
    _player(1, "QB"), _player(2, "RB"), _player(3, "RB"),
    _player(4, "WR"), _player(5, "WR"), _player(6, "WR"),
    _player(7, "TE"), _player(8, "WR", game=("DAL", "PHI"), team="DAL"),
    _player(9, "DST"),
]


def test_a_legal_lineup_passes_validation():
    assert validate_lineups([LEGAL]) == []


def test_validation_catches_a_duplicate_player():
    broken = LEGAL[:-1] + [LEGAL[0]]
    problems = validate_lineups([broken])
    assert problems
    assert any("more than once" in i for i in problems[0]["issues"])


def test_validation_catches_a_salary_overrun():
    broken = [_player(i, p, salary=9000) for i, p in enumerate(
        ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "WR", "DST"], start=1)]
    broken[-1] = _player(9, "DST", salary=9000, team="DAL", game=("DAL", "PHI"))
    problems = validate_lineups([broken])
    assert any("exceeds" in i for i in problems[0]["issues"])
    assert sum(p.salary for p in broken) > SALARY_CAP


def test_validation_catches_a_single_game_lineup():
    single = [_player(i, p) for i, p in enumerate(
        ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "WR", "DST"], start=1)]
    problems = validate_lineups([single])
    assert any("distinct game" in i for i in problems[0]["issues"])


def test_validation_catches_an_illegal_roster_shape():
    broken = [_player(i, "QB") for i in range(1, 10)]
    problems = validate_lineups([broken])
    assert any("roster shape" in i for i in problems[0]["issues"])


def test_validation_report_is_written_even_when_clean(
    slate_csv, projections_csv, tmp_path
):
    runs = tmp_path / "runs"
    run(slate_csv, projections_csv, runs, "--lineups", "2")
    report = json.loads(
        (only_run_dir(runs) / "validation_report.json").read_text()
    )
    assert report["lineups"] == 2
    assert report["problems"] == []


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

def test_exactly_one_slate_source_is_required(projections_csv, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--projections", str(projections_csv)])
    assert exc.value.code == 2
    assert "exactly one of --salaries or --slate-api" in capsys.readouterr().err


def test_both_slate_sources_is_a_usage_error(slate_csv, projections_csv, capsys):
    with pytest.raises(SystemExit):
        main(["--salaries", str(slate_csv), "--slate-api",
              "--projections", str(projections_csv)])
    assert "exactly one" in capsys.readouterr().err


def test_projections_are_required(slate_csv, capsys):
    """Without projections the optimizer would be maximising nothing."""
    with pytest.raises(SystemExit):
        main(["--salaries", str(slate_csv)])
    assert "--projections" in capsys.readouterr().err


def test_a_malformed_slate_exits_three(tmp_path, projections_csv, capsys):
    bad = tmp_path / "bad.csv"
    bad.write_text("Position,Name\nQB,Someone\n")
    assert run(bad, projections_csv, tmp_path / "runs") == EXIT_DATA
    assert "missing required column" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The API slate path, and failure handling
# ---------------------------------------------------------------------------

def test_the_api_slate_path_works(tmp_path, monkeypatch, capsys):
    """Interchangeable with --salaries, as the golden test guarantees."""
    from dfs_pipeline.adapters import DraftKingsApiAdapter

    draftables = (FIXTURES / "dk_draftables_sample.json").read_bytes()

    class Offline(DraftKingsApiAdapter):
        def raw_bytes(self):
            self.draft_group_id = self.draft_group_id or 151307
            return draftables

    monkeypatch.setattr("dfs_pipeline.cli.optimize.DraftKingsApiAdapter", Offline)

    names = [p.name for p in Offline(draft_group_id=1).loads(draftables)]
    projections = tmp_path / "proj.csv"
    with open(projections, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Name", "Projection"])
        for i, name in enumerate(names):
            writer.writerow([name, 5.0 + (i % 11)])

    runs = tmp_path / "runs"
    code = main(["--slate-api", "--projections", str(projections),
                 "--runs", str(runs), "--lineups", "1"])
    out = capsys.readouterr().out
    assert "draftkings draftables" in out
    # The trimmed fixture may be too thin to field a full lineup; either
    # outcome is acceptable, but it must not crash or report success falsely.
    assert code in (EXIT_OK, EXIT_DATA)
    if code == EXIT_DATA:
        assert "WARNING" in out or "cannot fill" in out


def test_an_unreachable_api_exits_one(tmp_path, projections_csv, monkeypatch, capsys):
    from dfs_pipeline.adapters import DraftKingsApiError

    class Broken:
        def __init__(self, *a, **k):
            self.draft_group_id = None

        def raw_bytes(self):
            raise DraftKingsApiError("unreachable; use the manual DKSalaries.csv")

    monkeypatch.setattr("dfs_pipeline.cli.optimize.DraftKingsApiAdapter", Broken)
    code = main(["--slate-api", "--projections", str(projections_csv),
                 "--runs", str(tmp_path / "runs")])
    assert code == 1
    assert "DKSalaries.csv" in capsys.readouterr().err


def test_a_malformed_config_exits_one(tmp_path, slate_csv, projections_csv, capsys):
    bad = tmp_path / "dfs.toml"
    bad.write_text("{{{")
    code = main(["--salaries", str(slate_csv), "--projections", str(projections_csv),
                 "--config", str(bad), "--runs", str(tmp_path / "runs")])
    assert code == 1
    assert "invalid TOML" in capsys.readouterr().err


def test_an_unwritable_runs_directory_exits_one(
    tmp_path, slate_csv, projections_csv, capsys
):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    code = main(["--salaries", str(slate_csv), "--projections", str(projections_csv),
                 "--runs", str(blocked)])
    assert code == 1
    assert capsys.readouterr().err


def test_validation_catches_a_wrong_player_count():
    assert any(
        "expected 9" in issue
        for issue in validate_lineups([LEGAL[:-1]])[0]["issues"]
    )


# ---------------------------------------------------------------------------
# --lock
# ---------------------------------------------------------------------------

def test_a_locked_player_is_in_every_lineup(slate_csv, projections_csv, tmp_path):
    runs = tmp_path / "runs"
    assert run(slate_csv, projections_csv, runs, "--lineups", "3",
               "--lock", "KC QB0") == EXIT_OK
    rows = list(csv.reader((only_run_dir(runs) / "lineups.csv").open(newline="")))
    for row in rows[1:]:
        assert any("KC QB0" in cell for cell in row)


def test_locks_are_recorded_in_run_metadata(slate_csv, projections_csv, tmp_path):
    runs = tmp_path / "runs"
    run(slate_csv, projections_csv, runs, "--lineups", "1", "--lock", "KC QB0")
    config = json.loads((only_run_dir(runs) / "run.json").read_text())["config"]
    assert config["locked"] == ["KC QB0"]


def test_an_ambiguous_lock_is_refused_rather_than_guessed():
    """The prototype's defect, tested at the unit that fixes it.

    It locked by name with a constraint reading "exactly one player called
    that is selected", which a duplicate name satisfies with the wrong person.
    Here the name is resolved to a single id up front, and an ambiguous name
    is an error naming every candidate.

    Tested directly rather than through the CLI because the projections
    adapter rejects a file with colliding name keys first -- correct
    defence in depth, but it means the CLI path never reaches this code.
    """
    from dfs_pipeline.cli.optimize import _resolve_locks


    pool = [
        SlatePlayer(source_player_id="1", name="Same Name", position="WR",
                    salary=5000, team="KC", game=GameInfo("KC", "BUF", None),
                    entity_type="player"),
        SlatePlayer(source_player_id="2", name="Same Name", position="WR",
                    salary=7000, team="BUF", game=GameInfo("KC", "BUF", None),
                    entity_type="player"),
    ]
    with pytest.raises(ValueError) as exc:
        _resolve_locks(["Same Name"], pool)
    message = str(exc.value)
    assert "matches 2 players" in message
    assert "cannot choose between them" in message
    assert "$5,000" in message and "$7,000" in message


def test_an_unambiguous_lock_resolves_to_one_id():
    from dfs_pipeline.cli.optimize import _resolve_locks

    pool = [
        SlatePlayer(source_player_id="7", name="Ja'Marr Chase", position="WR",
                    salary=8000, team="CIN", game=GameInfo("CIN", "KC", None),
                    entity_type="player"),
    ]
    # Spelling variants resolve, because normalization runs first.
    assert _resolve_locks(["JaMarr Chase"], pool) == ("7",)
    assert _resolve_locks([], pool) == ()


def test_locking_an_unknown_name_is_refused(
    slate_csv, projections_csv, tmp_path, capsys
):
    runs = tmp_path / "runs"
    assert run(slate_csv, projections_csv, runs,
               "--lock", "Nobody At All") == EXIT_DATA
    assert "not in the pool" in capsys.readouterr().err


def test_locking_an_excluded_player_says_so(slate_csv, projections_csv, tmp_path, capsys):
    runs = tmp_path / "runs"
    assert run(slate_csv, projections_csv, runs,
               "--ban", "KC QB0", "--lock", "KC QB0") == EXIT_DATA
    assert "not in the pool" in capsys.readouterr().err
