"""Tests for the ``dfs-snapshot`` command, config loading, and run directories.

The CLI is where the pieces become a thing you can actually run on a Sunday
morning, so these tests care as much about the failure paths as the happy one:
correct exit codes, actionable messages, and an audit trail that survives a
failed run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dfs_pipeline.cli.snapshot import EXIT_DATA, EXIT_ERROR, EXIT_OK, main
from dfs_pipeline.config import ConfigError, load_config
from dfs_pipeline.runs import RunDirectory
from dfs_pipeline.store import SnapshotStore

FIXTURES = Path(__file__).parent / "fixtures"
GOOD = str(FIXTURES / "dk_salaries_good.csv")
ONE_GAME = str(FIXTURES / "dk_salaries_one_game.csv")


@pytest.fixture()
def paths(tmp_path):
    return {
        "store": str(tmp_path / "snapshots.sqlite"),
        "runs": str(tmp_path / "runs"),
    }


def _capture(paths, *extra, salaries=GOOD):
    return main(
        [
            "--salaries", salaries,
            "--store", paths["store"],
            "--runs", paths["runs"],
            *extra,
        ]
    )


def _only_run_dir(paths) -> Path:
    dirs = sorted(Path(paths["runs"]).iterdir())
    assert len(dirs) == 1, f"expected one run directory, found {dirs}"
    return dirs[0]


def _run_json(path: Path) -> dict:
    return json.loads((path / "run.json").read_text())


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_capture_succeeds(paths, capsys):
    assert _capture(paths, "--captured-at", "2026-09-11T18:00:00Z") == EXIT_OK
    out = capsys.readouterr().out
    assert "Captured 13 entries" in out
    assert "observations" in out


def test_capture_writes_to_the_store(paths):
    _capture(paths, "--captured-at", "2026-09-11T18:00:00Z")
    with SnapshotStore.open(paths["store"], create=False) as store:
        assert store.observation_count() > 0
        assert store.artifact_count() == 1
        salaries = store.as_of("2026-09-12T00:00:00Z", metric="dk_salary")
        assert len(salaries) == 13


def test_success_is_never_silent(paths, capsys):
    """A command that prints nothing on success is indistinguishable from a no-op."""
    _capture(paths)
    assert capsys.readouterr().out.strip(), "successful capture printed nothing"


# ---------------------------------------------------------------------------
# The run directory: the audit trail
# ---------------------------------------------------------------------------

def test_run_directory_records_input_hash(paths):
    _capture(paths, "--captured-at", "2026-09-11T18:00:00Z")
    record = _run_json(_only_run_dir(paths))

    assert record["outcome"] == "success"
    assert len(record["inputs"]) == 1
    source = record["inputs"][0]
    assert source["filename"] == "dk_salaries_good.csv"
    assert len(source["sha256"]) == 64
    assert source["byte_size"] > 0


def test_run_directory_records_config_and_its_origins(paths):
    _capture(paths)
    record = _run_json(_only_run_dir(paths))
    assert record["config"]["origins"]["store.path"] == "command line"
    assert record["config"]["origins"]["capture.on_duplicate"] == "default"


def test_run_directory_records_environment(paths):
    """Reproducing a run months later needs to know what it ran on."""
    _capture(paths)
    record = _run_json(_only_run_dir(paths))
    assert record["package_version"] == "0.1.0"
    assert record["python_version"].startswith("3.")
    assert record["platform"]


def test_run_records_absence_of_randomness_explicitly(paths):
    """Stated, not omitted, so determinism is confirmable rather than assumed."""
    _capture(paths)
    assert _run_json(_only_run_dir(paths))["randomness"] == "none"


def test_failed_run_still_writes_its_directory(paths):
    """A run that vanishes when it breaks cannot be debugged afterwards."""
    assert _capture(paths, salaries=ONE_GAME) == EXIT_DATA
    record = _run_json(_only_run_dir(paths))
    assert record["outcome"] == "failed"
    assert "only one game present" in record["error"]
    assert record["finished_at"] is not None


def test_run_log_is_written_regardless_of_verbosity(paths):
    """The console is quiet by default; the log keeps everything anyway.

    Otherwise diagnosing a Week 7 failure depends on having thought to pass
    -v in Week 7.
    """
    _capture(paths)
    log = (_only_run_dir(paths) / "run.log").read_text()
    assert "run" in log and "started" in log


def test_runs_in_the_same_second_do_not_overwrite_each_other(paths):
    """Regression: run ids are timestamped to the second.

    Two runs started within the same second previously shared a directory,
    and the second silently destroyed the first's metadata. Re-running a
    failed capture immediately is entirely ordinary, so this collision was
    real rather than theoretical.
    """
    for _ in range(4):
        _capture(paths, salaries=ONE_GAME)
    assert len(list(Path(paths["runs"]).iterdir())) == 4


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------

def test_malformed_input_exits_three_with_a_specific_message(paths, capsys):
    assert _capture(paths, salaries=ONE_GAME) == EXIT_DATA
    err = capsys.readouterr().err
    assert "only one game present" in err
    assert "nothing was written" in err


def test_missing_file_is_reported_not_traced(paths, capsys, tmp_path):
    assert _capture(paths, salaries=str(tmp_path / "absent.csv")) == EXIT_DATA
    assert "does not exist" in capsys.readouterr().err


def test_duplicate_capture_is_refused_with_a_remedy(paths, capsys):
    _capture(paths, "--captured-at", "2026-09-11T18:00:00Z")
    capsys.readouterr()
    assert _capture(paths, "--captured-at", "2026-09-11T18:00:00Z") == EXIT_DATA
    err = capsys.readouterr().err
    assert "already captured" in err
    assert "--on-duplicate ignore" in err, "error should name the remedy"


def test_duplicate_capture_can_be_ignored(paths):
    _capture(paths, "--captured-at", "2026-09-11T18:00:00Z")
    assert _capture(
        paths, "--captured-at", "2026-09-11T18:00:00Z", "--on-duplicate", "ignore"
    ) == EXIT_OK


def test_no_source_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
    assert "nothing to capture" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def test_dry_run_validates_without_writing(paths, capsys, tmp_path):
    assert _capture(paths, "--dry-run") == EXIT_OK
    out = capsys.readouterr().out
    assert "is valid" in out
    assert "nothing was written" in out.lower()
    assert not Path(paths["store"]).exists(), "dry run created a store"
    assert not Path(paths["runs"]).exists(), "dry run created a run directory"


def test_dry_run_rejects_bad_input(paths, capsys):
    assert _capture(paths, "--dry-run", salaries=ONE_GAME) == EXIT_DATA
    assert "only one game present" in capsys.readouterr().err


def test_dry_run_reports_flagged_players(paths, capsys):
    _capture(paths, "--dry-run", salaries=str(FIXTURES / "dk_salaries_real_shape.csv"))
    assert "flagged" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

def test_effective_at_defaults_to_captured_at(paths):
    _capture(paths, "--captured-at", "2026-09-11T18:00:00Z")
    results = _run_json(_only_run_dir(paths))["results"]
    assert results["effective_at"] == results["captured_at"] == "2026-09-11T18:00:00Z"


def test_effective_at_can_be_set_for_backfill(paths):
    _capture(
        paths,
        "--captured-at", "2026-09-13T09:00:00Z",
        "--effective-at", "2026-09-10T12:00:00Z",
    )
    results = _run_json(_only_run_dir(paths))["results"]
    assert results["effective_at"] == "2026-09-10T12:00:00Z"
    assert results["captured_at"] == "2026-09-13T09:00:00Z"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_defaults_apply_with_no_config_file(tmp_path):
    config = load_config(search_from=tmp_path)
    assert config.store_path == Path("data/snapshots.sqlite")
    assert config.on_duplicate == "error"
    assert config.source_file is None


def test_config_file_is_discovered(tmp_path):
    (tmp_path / "dfs.toml").write_text(
        '[store]\npath = "custom/place.sqlite"\n'
    )
    config = load_config(search_from=tmp_path)
    assert config.store_path == Path("custom/place.sqlite")
    assert config.origins["store.path"] == "config file"


def test_command_line_beats_config_file(tmp_path):
    (tmp_path / "dfs.toml").write_text('[store]\npath = "from/config.sqlite"\n')
    config = load_config(
        search_from=tmp_path, overrides={"store_path": "from/cli.sqlite"}
    )
    assert config.store_path == Path("from/cli.sqlite")
    assert config.origins["store.path"] == "command line"


def test_absent_override_does_not_clobber_config(tmp_path):
    """A flag that was not passed must leave the config value alone."""
    (tmp_path / "dfs.toml").write_text('[store]\npath = "from/config.sqlite"\n')
    config = load_config(search_from=tmp_path, overrides={"store_path": None})
    assert config.store_path == Path("from/config.sqlite")


def test_malformed_config_is_a_hard_failure(tmp_path):
    """Falling back to defaults would run against the wrong store, silently."""
    (tmp_path / "dfs.toml").write_text("this is not valid toml {{{")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(search_from=tmp_path)


def test_unknown_config_section_is_rejected(tmp_path):
    """Catches a typo'd section that would otherwise be silently ignored."""
    (tmp_path / "dfs.toml").write_text('[stoer]\npath = "x"\n')
    with pytest.raises(ConfigError, match="unknown section"):
        load_config(search_from=tmp_path)


def test_unknown_config_key_is_rejected(tmp_path):
    (tmp_path / "dfs.toml").write_text('[store]\nptah = "x"\n')
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(search_from=tmp_path)


def test_invalid_on_duplicate_is_rejected(tmp_path):
    (tmp_path / "dfs.toml").write_text('[capture]\non_duplicate = "maybe"\n')
    with pytest.raises(ConfigError, match="on_duplicate"):
        load_config(search_from=tmp_path)


def test_explicit_missing_config_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_config_section_must_be_a_table(tmp_path):
    (tmp_path / "dfs.toml").write_text('store = "not a table"\n')
    with pytest.raises(ConfigError, match="must be a table"):
        load_config(search_from=tmp_path)


def test_show_config_explains_where_values_came_from(tmp_path, capsys):
    (tmp_path / "dfs.toml").write_text('[store]\npath = "configured.sqlite"\n')
    assert main(["--config", str(tmp_path / "dfs.toml"), "--show-config"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "configured.sqlite" in out
    assert "config file" in out
    assert "default" in out


def test_bad_config_from_cli_exits_nonzero(tmp_path, capsys):
    bad = tmp_path / "dfs.toml"
    bad.write_text("{{{")
    assert main(["--config", str(bad), "--salaries", GOOD]) == EXIT_ERROR
    assert "invalid TOML" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# RunDirectory in isolation
# ---------------------------------------------------------------------------

def test_unwritable_store_path_exits_one_not_three(paths, capsys, tmp_path):
    """Environment failures are distinct from bad data, and exit differently.

    A caller scripting this needs to tell "your CSV is wrong" (fix the file)
    from "the disk is unavailable" (fix the machine).
    """
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    assert main(
        ["--salaries", GOOD, "--store", str(blocked), "--runs", paths["runs"]]
    ) == EXIT_ERROR
    assert capsys.readouterr().err


def test_unreadable_config_is_reported(tmp_path):
    """A config that exists but cannot be read must not fall back silently.

    Falling back to defaults here would run against a different store than
    the operator configured, and say nothing about it.
    """
    target = tmp_path / "dfs.toml"
    target.write_text('[store]\npath = "x.sqlite"\n')
    target.chmod(0o000)
    try:
        with pytest.raises(ConfigError, match="cannot read"):
            load_config(search_from=tmp_path)
    finally:
        target.chmod(0o644)  # so tmp_path cleanup succeeds


def test_run_directory_gives_up_loudly_after_too_many_collisions(tmp_path):
    runs = tmp_path / "runs"
    first = RunDirectory(runs, command="test")
    with first:
        pass
    clash = RunDirectory(runs, command="test")
    clash.path = first.path  # force the same name
    with pytest.raises(OSError, match="unique run directory"):
        clash._claim_unique_directory(limit=1)


def test_run_directory_propagates_exceptions(tmp_path):
    """Recording a failure must not swallow it."""
    with pytest.raises(ValueError, match="boom"):
        with RunDirectory(tmp_path / "runs", command="test"):
            raise ValueError("boom")
    record = json.loads(
        next((tmp_path / "runs").iterdir()).joinpath("run.json").read_text()
    )
    assert record["outcome"] == "failed"
    assert "boom" in record["error"]
