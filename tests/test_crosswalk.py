"""Tests for identity resolution.

The reference index is constructed explicitly here rather than loaded from
nflverse, so no test touches the network. The behaviours under test are the
ones that made the prototype fail silently: what happens on a miss, what
happens on an ambiguity, and whether a decision once made is kept.
"""

from __future__ import annotations

import pytest

from dfs_pipeline.adapters.base import GameInfo, SlatePlayer
from dfs_pipeline.crosswalk import (
    MATCH_METHODS,
    SUBJECT_SALARY_FLOOR,
    IdentityResolver,
    MatchReport,
    ReferencePlayer,
)
from dfs_pipeline.names import normalize_name
from dfs_pipeline.store import SnapshotStore


def ref(nflverse_id, name, team=None, position=None, table="ff_playerids"):
    return ReferencePlayer(
        nflverse_id=nflverse_id, name=name, normalized=normalize_name(name),
        team=team, position=position, source_table=table,
    )


REFERENCE = [
    ref("00-0033873", "Patrick Mahomes", "KC", "QB"),
    ref("00-0036919", "Kenneth Gainwell", "TB", "RB"),
    # The SAME player under a second spelling -- both must be indexed.
    ref("00-0036919", "Kenny Gainwell", "TB", "RB", table="players"),
    ref("00-0034796", "Ja'Marr Chase", "CIN", "WR"),
    # Two genuinely different people who share a name.
    ref("00-0031381", "Michael Thomas", "NO", "WR"),
    ref("00-0032235", "Michael Thomas", "HOU", "DB"),
]


@pytest.fixture()
def resolver():
    return IdentityResolver(REFERENCE)


@pytest.fixture()
def store(tmp_path):
    with SnapshotStore.open(tmp_path / "snapshots.sqlite") as s:
        yield s


def player(name, *, pid="1", position="WR", team="KC", salary=5000,
           entity_type="player"):
    return SlatePlayer(
        source_player_id=pid, name=name, position=position, salary=salary,
        team=team, game=GameInfo("KC", "BUF", None), entity_type=entity_type,
    )


# ---------------------------------------------------------------------------
# The alias problem this module exists for
# ---------------------------------------------------------------------------

def test_a_player_resolves_under_either_spelling(resolver):
    """Regression: deduplicating the reference by id discarded aliases.

    ff_playerids calls him "Kenneth Gainwell"; `players` and DraftKings say
    "Kenny Gainwell". Both carry gsis 00-0036919. An index keyed on id alone
    kept the first spelling and dropped the second -- throwing away precisely
    the aliases this module exists to resolve.
    """
    for spelling in ("Kenneth Gainwell", "Kenny Gainwell"):
        outcome = resolver.resolve(
            source="DK", source_subject_id="x", name=spelling,
            team="TB", position="RB",
        )
        assert outcome.nflverse_id == "00-0036919", spelling


def test_multiple_spellings_of_one_player_are_not_ambiguity(resolver):
    """Two reference rows, one person: that resolves, it does not fail."""
    outcome = resolver._resolve_player("Kenny Gainwell", "TB", "RB")
    assert outcome.resolved
    assert outcome.confidence == 1.0


def test_punctuation_variants_resolve(resolver):
    """An apostrophe is noise; a space is not."""
    for spelling in ("Ja'Marr Chase", "JaMarr Chase", "JA'MARR CHASE"):
        assert resolver._resolve_player(spelling, "CIN", "WR").nflverse_id == (
            "00-0034796"
        ), spelling


def test_a_space_is_not_treated_as_punctuation(resolver):
    """"Ja Marr Chase" deliberately does NOT match "Ja'Marr Chase".

    Deleting apostrophes is safe -- sources disagree about them constantly.
    Collapsing spaces would not be: it would merge genuinely distinct names,
    and merging two people is far worse than failing to match one. This miss
    is visible in the report and resolvable by hand once.
    """
    assert not resolver._resolve_player("Ja Marr Chase", "CIN", "WR").resolved


# ---------------------------------------------------------------------------
# Genuine ambiguity
# ---------------------------------------------------------------------------

def test_shared_names_are_separated_by_team_and_position(resolver):
    receiver = resolver._resolve_player("Michael Thomas", "NO", "WR")
    defender = resolver._resolve_player("Michael Thomas", "HOU", "DB")
    assert receiver.nflverse_id == "00-0031381"
    assert defender.nflverse_id == "00-0032235"
    assert receiver.confidence == 0.9, "a disambiguated match is less certain"


def test_shared_names_that_cannot_be_separated_stay_unresolved(resolver):
    """Guessing would silently attach one player's history to another."""
    outcome = resolver._resolve_player("Michael Thomas", None, None)
    assert not outcome.resolved
    assert "share this name" in outcome.note


def test_position_alone_can_disambiguate(resolver):
    """A player's team changes mid-season more often than their position."""
    outcome = resolver._resolve_player("Michael Thomas", "XXX", "WR")
    assert outcome.nflverse_id == "00-0031381"


# ---------------------------------------------------------------------------
# Misses are recorded, never guessed
# ---------------------------------------------------------------------------

def test_an_unknown_name_resolves_to_nothing(resolver):
    outcome = resolver._resolve_player("Nobody Here", "KC", "WR")
    assert not outcome.resolved
    assert outcome.match_method == "unresolved"
    assert outcome.note


def test_there_is_no_fuzzy_matching(resolver):
    """A deliberate decision, asserted so it cannot drift in unnoticed.

    Exact matching plus team/position disambiguation reaches 98% on a real
    slate, and the residue is the cheapest players on it. Fuzzy matching there
    would trade a visible miss for an invisible wrong answer.
    """
    for near_miss in ("Patrick Mahomez", "Patrik Mahomes", "P Mahomes"):
        assert not resolver._resolve_player(near_miss, "KC", "QB").resolved


# ---------------------------------------------------------------------------
# Defenses never touch name logic
# ---------------------------------------------------------------------------

def test_defenses_resolve_through_the_team_map(resolver):
    outcome = resolver.resolve(
        source="DK", source_subject_id="d1", name="Chiefs",
        entity_type="dst", team="KC",
    )
    assert outcome.nflverse_id == "KC"
    assert outcome.match_method == "dst_alias"
    assert outcome.confidence == 1.0


def test_a_defense_resolves_from_its_nickname_alone(resolver):
    outcome = resolver.resolve(
        source="DK", source_subject_id="d2", name="Jaguars", entity_type="dst",
    )
    assert outcome.nflverse_id == "JAX"


def test_a_defense_is_never_matched_against_player_names(resolver):
    """A defense is a team-level entity, not a player with an odd name."""
    outcome = resolver.resolve(
        source="DK", source_subject_id="d3", name="Patrick Mahomes",
        entity_type="dst", team=None,
    )
    assert not outcome.resolved
    assert outcome.match_method == "unresolved"


def test_an_unknown_defense_fails_rather_than_guessing(resolver):
    outcome = resolver.resolve(
        source="DK", source_subject_id="d4", name="Huskies", entity_type="dst",
    )
    assert not outcome.resolved


# ---------------------------------------------------------------------------
# Persistence: decide once, keep the answer
# ---------------------------------------------------------------------------

def test_resolutions_are_persisted(store):
    resolver = IdentityResolver(REFERENCE, store=store)
    resolver.resolve(source="DK", source_subject_id="42", name="Patrick Mahomes",
                     team="KC", position="QB")
    row = store._con.execute(
        "SELECT * FROM crosswalk WHERE source_subject_id = '42'"
    ).fetchone()
    assert row["nflverse_id"] == "00-0033873"
    assert row["match_method"] == "normalized"
    assert row["review_status"] == "pending"
    assert row["first_seen"] and row["last_seen"]


def test_a_stored_answer_is_reused_not_recomputed(store):
    """Not merely an optimisation.

    A name match is fallible, so deciding once and keeping the answer means a
    player cannot resolve one way in Week 3 and another in Week 12 because an
    upstream table shifted underneath us.
    """
    resolver = IdentityResolver(REFERENCE, store=store)
    resolver.resolve(source="DK", source_subject_id="42", name="Patrick Mahomes",
                     team="KC", position="QB")

    # Empty the reference entirely; the stored answer must still stand.
    starved = IdentityResolver([], store=store)
    outcome = starved.resolve(source="DK", source_subject_id="42",
                              name="Patrick Mahomes", team="KC", position="QB")
    assert outcome.nflverse_id == "00-0033873"
    assert "stored crosswalk" in outcome.note


def test_a_miss_is_stored_too(store):
    """So the same fruitless lookup is not repeated every week."""
    resolver = IdentityResolver(REFERENCE, store=store)
    resolver.resolve(source="DK", source_subject_id="99", name="Nobody Here",
                     team="KC", position="WR")
    row = store._con.execute(
        "SELECT nflverse_id, match_method FROM crosswalk "
        "WHERE source_subject_id = '99'"
    ).fetchone()
    assert row["nflverse_id"] is None
    assert row["match_method"] == "unresolved"


def test_a_rejected_resolution_is_not_silently_redone(store):
    """A human looked at this and said no; re-deriving undoes their decision."""
    resolver = IdentityResolver(REFERENCE, store=store)
    resolver.resolve(source="DK", source_subject_id="42", name="Patrick Mahomes",
                     team="KC", position="QB")
    store._con.execute(
        "UPDATE crosswalk SET review_status = 'rejected' "
        "WHERE source_subject_id = '42'"
    )
    outcome = resolver.resolve(source="DK", source_subject_id="42",
                               name="Patrick Mahomes", team="KC", position="QB")
    assert not outcome.resolved
    assert "rejected" in outcome.note


def test_reresolving_updates_last_seen_without_duplicating(store):
    resolver = IdentityResolver(REFERENCE, store=store)
    for _ in range(3):
        resolver.resolve(source="DK", source_subject_id="42",
                         name="Patrick Mahomes", team="KC", position="QB")
    count = store._con.execute(
        "SELECT COUNT(*) FROM crosswalk WHERE source_subject_id = '42'"
    ).fetchone()[0]
    assert count == 1


def test_different_sources_keep_separate_rows(store):
    resolver = IdentityResolver(REFERENCE, store=store)
    resolver.resolve(source="DK", source_subject_id="42", name="Patrick Mahomes",
                     team="KC", position="QB")
    resolver.resolve(source="DFF", source_subject_id="patrick mahomes",
                     name="Patrick Mahomes", team="KC", position="QB")
    rows = store._con.execute("SELECT source FROM crosswalk").fetchall()
    assert {r["source"] for r in rows} == {"DK", "DFF"}


def test_resolution_works_without_a_store(resolver):
    """The store is optional; resolution must not require persistence."""
    assert resolver.resolve(
        source="DK", source_subject_id="1", name="Patrick Mahomes",
        team="KC", position="QB",
    ).resolved


# ---------------------------------------------------------------------------
# The match report: never silent
# ---------------------------------------------------------------------------

def test_report_counts_by_method(resolver):
    slate = [
        player("Patrick Mahomes", pid="1", position="QB"),
        player("Chiefs", pid="2", position="DST", entity_type="dst"),
        player("Nobody Here", pid="3"),
    ]
    _, report = resolver.resolve_slate(slate, source="DK")
    assert report.total == 3
    assert report.resolved == 2
    assert report.by_method["normalized"] == 1
    assert report.by_method["dst_alias"] == 1
    assert report.by_method["unresolved"] == 1
    assert report.match_rate == pytest.approx(2 / 3)


def test_expensive_misses_are_singled_out(resolver):
    """A missing $3,000 punt is noise; a missing $8,000 player is a hole."""
    slate = [
        player("Nobody Cheap", pid="1", salary=3000),
        player("Nobody Costly", pid="2", salary=8000),
    ]
    _, report = resolver.resolve_slate(slate, source="DK")
    gaps = report.expensive_misses()
    assert [g["name"] for g in gaps] == ["Nobody Costly"]
    assert SUBJECT_SALARY_FLOOR == 5000


def test_the_report_renders_the_warning(resolver):
    slate = [player("Nobody Costly", pid="1", salary=8000)]
    _, report = resolver.resolve_slate(slate, source="DK")
    text = report.render()
    assert "WARNING" in text
    assert "Nobody Costly" in text
    assert "8,000" in text


def test_a_clean_report_has_no_warning(resolver):
    slate = [player("Patrick Mahomes", pid="1", position="QB")]
    _, report = resolver.resolve_slate(slate, source="DK")
    assert "WARNING" not in report.render()
    assert "100.0%" in report.render()


def test_ambiguities_are_reported_separately(resolver):
    slate = [player("Michael Thomas", pid="1", position="XX", team="XXX")]
    _, report = resolver.resolve_slate(slate, source="DK")
    assert len(report.ambiguous) == 1
    assert "could not be separated" in report.render()


def test_an_empty_report_does_not_divide_by_zero():
    assert MatchReport().match_rate == 0.0


def test_every_method_used_is_a_declared_method(resolver):
    slate = [
        player("Patrick Mahomes", pid="1", position="QB"),
        player("Chiefs", pid="2", position="DST", entity_type="dst"),
        player("Nobody", pid="3"),
    ]
    _, report = resolver.resolve_slate(slate, source="DK")
    assert set(report.by_method) <= set(MATCH_METHODS)


# ---------------------------------------------------------------------------
# Building the reference from nflverse (stubbed -- no network)
# ---------------------------------------------------------------------------

def _stub_nflverse(monkeypatch, ff_rows, player_rows):
    import sys
    import types

    import polars as pl

    monkeypatch.setitem(sys.modules, "nflreadpy", types.SimpleNamespace(
        load_ff_playerids=lambda: pl.DataFrame(ff_rows),
        load_players=lambda: pl.DataFrame(player_rows),
    ))


def test_both_reference_tables_are_layered(monkeypatch):
    """`players` is broader and catches what the fantasy table omits.

    Measured on a real slate: ff_playerids alone resolved 82.9%; layering
    players took it to 98%.
    """
    _stub_nflverse(
        monkeypatch,
        [{"name": "Patrick Mahomes", "position": "QB", "team": "KCC",
          "gsis_id": "00-0033873"}],
        [{"display_name": "Rookie Newman", "position": "WR",
          "gsis_id": "00-0099999", "latest_team": "KC"}],
    )
    resolver = IdentityResolver.from_nflverse()
    assert resolver._resolve_player("Patrick Mahomes", "KC", "QB").resolved
    assert resolver._resolve_player("Rookie Newman", "KC", "WR").resolved


def test_alternate_spellings_across_tables_are_both_indexed(monkeypatch):
    """The Gainwell case, at the loader level."""
    _stub_nflverse(
        monkeypatch,
        [{"name": "Kenneth Gainwell", "position": "RB", "team": "TBB",
          "gsis_id": "00-0036919"}],
        [{"display_name": "Kenny Gainwell", "position": "RB",
          "gsis_id": "00-0036919", "latest_team": "TB"}],
    )
    resolver = IdentityResolver.from_nflverse()
    for spelling in ("Kenneth Gainwell", "Kenny Gainwell"):
        assert resolver._resolve_player(spelling, "TB", "RB").nflverse_id == (
            "00-0036919"
        ), spelling


def test_rows_without_a_gsis_id_are_skipped(monkeypatch):
    """An unidentified player cannot anchor a resolution."""
    _stub_nflverse(
        monkeypatch,
        [{"name": "Ghost Player", "position": "WR", "team": "KC",
          "gsis_id": None}],
        [{"display_name": "Real Player", "position": "WR",
          "gsis_id": "00-0000001", "latest_team": "KC"}],
    )
    resolver = IdentityResolver.from_nflverse()
    assert not resolver._resolve_player("Ghost Player", "KC", "WR").resolved
    assert resolver._resolve_player("Real Player", "KC", "WR").resolved


def test_free_agent_team_codes_become_no_team(monkeypatch):
    """nflverse writes "FA" for unrostered players; that is not a franchise."""
    _stub_nflverse(
        monkeypatch,
        [{"name": "Free Agent", "position": "WR", "team": "FA",
          "gsis_id": "00-0000002"}],
        [{"display_name": "Someone Else", "position": "WR",
          "gsis_id": "00-0000003", "latest_team": "KC"}],
    )
    resolver = IdentityResolver.from_nflverse()
    entry = resolver._by_name[normalize_name("Free Agent")][0]
    assert entry.team is None


def test_unrecognised_team_codes_become_no_team_rather_than_failing(monkeypatch):
    _stub_nflverse(
        monkeypatch,
        [{"name": "Odd Team", "position": "WR", "team": "ZZZ",
          "gsis_id": "00-0000004"}],
        [{"display_name": "Other", "position": "WR", "gsis_id": "00-0000005",
          "latest_team": "KC"}],
    )
    resolver = IdentityResolver.from_nflverse()
    entry = resolver._by_name[normalize_name("Odd Team")][0]
    assert entry.team is None
    assert resolver._resolve_player("Odd Team", "KC", "WR").resolved


def test_a_players_table_without_latest_team_still_loads(monkeypatch):
    _stub_nflverse(
        monkeypatch,
        [{"name": "A", "position": "WR", "team": "KC", "gsis_id": "00-1"}],
        [{"display_name": "B", "position": "WR", "gsis_id": "00-2"}],
    )
    resolver = IdentityResolver.from_nflverse()
    assert resolver._resolve_player("B", None, "WR").resolved
