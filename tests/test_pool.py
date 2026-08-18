"""Tests for player-pool filtering.

The behaviours that matter are the ones about *not knowing*: what happens when
a slate carries no status column, and what happens when a status arrives that
we do not recognise. Both are cases where a naive filter quietly does the
wrong thing.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from dfs_pipeline.pool import (
    CANONICAL_STATUSES,
    DEFAULT_EXCLUDED,
    STATUS_ALIASES,
    filter_pool,
    normalize_status,
    parse_status_list,
)

FIXTURES = Path(__file__).parent / "fixtures"


class Player:
    __slots__ = ("name", "status", "salary", "position")

    def __init__(self, name, status=None, salary=5000, position="WR"):
        self.name, self.status, self.salary, self.position = (
            name, status, salary, position
        )


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("OUT", "OUT"), ("O", "OUT"), ("out", "OUT"),
        ("Q", "QUESTIONABLE"), ("QUESTIONABLE", "QUESTIONABLE"),
        ("D", "DOUBTFUL"), ("DOUBTFUL", "DOUBTFUL"),
        ("IR", "IR"), ("ir", "IR"), ("Injured Reserve", "IR"),
        ("PUP", "PUP"), ("SUSP", "SUSPENDED"),
    ],
)
def test_source_spellings_normalize(raw, expected):
    assert normalize_status(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "  ", "None", "ACTIVE", "ACT", "-"])
def test_a_clear_player_has_no_status(raw):
    """DraftKings writes the literal string "None"; that is not a designation."""
    assert normalize_status(raw) is None


def test_draftkings_does_not_emit_doubtful():
    """The handoff's example filter assumed a spelling DK never uses.

    Verified against a real export: DK emits Q, IR and OUT only. A filter
    written to "OUT,DOUBTFUL" would have matched nothing for doubtful players
    and silently left them in the pool. Both spellings resolve here.
    """
    raw = (FIXTURES / "dk_salaries_real_shape.csv").read_bytes().decode("utf-8-sig")
    observed = {
        (r.get("Status") or "").strip()
        for r in csv.DictReader(io.StringIO(raw))
        if (r.get("Status") or "").strip()
    }
    assert observed <= {"Q", "IR", "OUT"}
    assert "DOUBTFUL" not in observed
    assert parse_status_list("OUT,DOUBTFUL") == {"OUT", "DOUBTFUL"}


def test_every_alias_maps_into_the_canonical_vocabulary():
    assert set(STATUS_ALIASES.values()) <= set(CANONICAL_STATUSES)


def test_parsing_a_filter_list():
    assert parse_status_list("out, q") == {"OUT", "QUESTIONABLE"}
    assert parse_status_list(["O", "IR"]) == {"OUT", "IR"}
    assert parse_status_list(None) == frozenset()
    assert parse_status_list("") == frozenset()


# ---------------------------------------------------------------------------
# Default behaviour
# ---------------------------------------------------------------------------

def test_unplayable_designations_are_excluded_by_default():
    players = [
        Player("Healthy"), Player("Ruled Out", "OUT"), Player("On IR", "IR"),
        Player("Suspended", "SUSP"),
    ]
    kept, report = filter_pool(players)
    assert [p.name for p in kept] == ["Healthy"]
    assert report.excluded == 3


def test_questionable_players_are_kept_by_default():
    """A questionable player is a judgement call, often the point of a
    contrarian lineup. Removing them by default would make that decision for
    the operator silently."""
    assert "QUESTIONABLE" not in DEFAULT_EXCLUDED
    kept, _ = filter_pool([Player("Gametime", "Q")])
    assert len(kept) == 1


def test_questionable_can_be_excluded_explicitly():
    kept, report = filter_pool(
        [Player("Gametime", "Q")], exclude_statuses="OUT,QUESTIONABLE"
    )
    assert kept == []
    assert report.excluded_by_status["QUESTIONABLE"] == ["Gametime"]


def test_status_filtering_can_be_disabled_entirely():
    kept, _ = filter_pool([Player("Ruled Out", "OUT")], exclude_statuses=[])
    assert len(kept) == 1


# ---------------------------------------------------------------------------
# Not knowing is not the same as everyone being healthy
# ---------------------------------------------------------------------------

def test_a_slate_with_no_status_data_says_so():
    """The handoff: "If absent, say so explicitly."."""
    kept, report = filter_pool([Player("A"), Player("B")])
    assert len(kept) == 2
    assert report.status_data_present is False
    assert "no status data" in report.render()
    assert "WARNING" in report.render()


def test_a_slate_with_status_data_does_not_warn():
    _, report = filter_pool([Player("A"), Player("Out", "OUT")])
    assert report.status_data_present is True
    assert "no status data" not in report.render()


def test_an_unrecognised_status_is_reported_not_acted_on():
    """Guessing an unknown code means "out" could silently empty a position."""
    kept, report = filter_pool([Player("Odd", "NFI"), Player("Healthy")])
    assert len(kept) == 2, "an unknown status must not remove a player"
    assert "NFI" in report.unknown_statuses
    assert "unrecognised status" in report.render()


def test_unknown_statuses_are_named_in_the_report():
    _, report = filter_pool([Player("Odd One", "ZZZ")])
    assert "ZZZ" in report.render()
    assert "Odd One" in report.render()


# ---------------------------------------------------------------------------
# Other filters
# ---------------------------------------------------------------------------

def test_a_salary_floor_excludes_and_reports():
    players = [Player("Cheap", salary=2500), Player("Costly", salary=9000)]
    kept, report = filter_pool(players, min_salary=3000)
    assert [p.name for p in kept] == ["Costly"]
    assert report.excluded_by_salary == ["Cheap"]


def test_named_players_can_be_banned():
    players = [Player("Keep Me"), Player("Drop Me")]
    kept, report = filter_pool(players, exclude_names=["drop me"])
    assert [p.name for p in kept] == ["Keep Me"]
    assert report.excluded_by_name == ["Drop Me"]


def test_banning_is_case_insensitive():
    kept, _ = filter_pool([Player("Josh Allen")], exclude_names=["JOSH ALLEN"])
    assert kept == []


def test_custom_accessors_are_supported():
    rows = [{"n": "A", "s": "OUT", "sal": 5000}, {"n": "B", "s": None, "sal": 5000}]
    kept, _ = filter_pool(
        rows,
        status_of=lambda r: r["s"], name_of=lambda r: r["n"],
        salary_of=lambda r: r["sal"],
    )
    assert [r["n"] for r in kept] == ["B"]


# ---------------------------------------------------------------------------
# The report is the point
# ---------------------------------------------------------------------------

def test_every_exclusion_is_named():
    """A pool that silently shrinks is indistinguishable from one that was
    always small."""
    players = [
        Player("Healthy"), Player("Ruled Out", "OUT"), Player("On IR", "IR"),
        Player("Cheap", salary=1000), Player("Banned"),
    ]
    _, report = filter_pool(players, min_salary=2000, exclude_names=["Banned"])
    text = report.render()
    for name in ("Ruled Out", "On IR"):
        assert name in text
    assert report.excluded == 4
    assert report.kept == 1


def test_the_report_counts_add_up():
    players = [Player(f"p{i}") for i in range(5)] + [Player("Out", "OUT")]
    _, report = filter_pool(players)
    assert report.considered == 6
    assert report.kept == 5
    assert report.excluded == 1


def test_an_empty_slate_does_not_explode():
    kept, report = filter_pool([])
    assert kept == []
    assert report.considered == 0
    assert "0/0" in report.render()


# ---------------------------------------------------------------------------
# Against the real slate
# ---------------------------------------------------------------------------

def test_the_real_slate_filters_as_expected():
    from dfs_pipeline.adapters import DraftKingsCsvAdapter

    slate = DraftKingsCsvAdapter(FIXTURES / "dk_salaries_real_shape.csv").load()
    kept, report = filter_pool(slate)
    assert report.status_data_present
    assert report.kept < report.considered, "fixture contains flagged players"
    assert set(report.excluded_by_status) <= set(CANONICAL_STATUSES)
    assert not report.unknown_statuses, "real DK statuses should all be known"


def test_long_exclusion_lists_are_truncated_with_a_count():
    """A report nobody reads because it is 200 lines long is not a report."""
    players = [Player(f"Out{i}", "OUT") for i in range(9)]
    _, report = filter_pool(players)
    text = report.render()
    assert "... and 4 more" in text
    assert text.count("Out") <= 8, "should not print all nine"
