"""Tests for the DraftKings draftables adapter.

Two jobs, in order of importance.

First, the **safety boundary**: this adapter must never authenticate, hold a
session, or mutate anything. That is asserted by reading the module's own
source, because a convention nobody checks is a convention that erodes.

Second, the **golden equivalence** the handoff requires: for every field both
paths can express, the API and the CSV must produce identical normalized
records for the same slate. Both fixtures are real captures of the 2026 Week 1
main slate (draft group 151307), so this compares reality against reality.

No test touches the network.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from dfs_pipeline.adapters import (
    ROSTER_SLOTS,
    DraftKingsApiAdapter,
    DraftKingsApiError,
    DraftKingsCsvAdapter,
    SlateSchemaError,
)
from dfs_pipeline.adapters.dk_api import (
    CLASSIC_GAME_TYPE_ID,
    FLEX_SLOT_ID,
    DraftGroup,
    _utc,
)
from dfs_pipeline.capture import ingest_slate
from dfs_pipeline.store import SnapshotStore

FIXTURES = Path(__file__).parent / "fixtures"
DRAFTABLES = FIXTURES / "dk_draftables_sample.json"
SALARIES = FIXTURES / "dk_salaries_real_shape.csv"


@pytest.fixture()
def adapter() -> DraftKingsApiAdapter:
    return DraftKingsApiAdapter(draft_group_id=151307)


@pytest.fixture()
def api_players(adapter):
    return adapter.loads(DRAFTABLES.read_bytes())


@pytest.fixture()
def csv_players():
    return DraftKingsCsvAdapter(SALARIES).load()


class OfflineApiAdapter(DraftKingsApiAdapter):
    """The adapter with fetching replaced by the recorded fixture.

    `ingest_slate` calls `raw_bytes()`, which on the real adapter performs a
    live request. Subclassing here keeps every line of parsing, normalization
    and validation under test while guaranteeing the suite never touches the
    network -- a test that reaches DraftKings is a test that fails on a plane,
    hammers someone else's servers, and changes behaviour week to week.
    """

    def raw_bytes(self) -> bytes:
        return DRAFTABLES.read_bytes()


@pytest.fixture()
def offline() -> OfflineApiAdapter:
    return OfflineApiAdapter(draft_group_id=151307)


@pytest.fixture()
def store(tmp_path):
    with SnapshotStore.open(tmp_path / "snapshots.sqlite") as s:
        yield s


# ---------------------------------------------------------------------------
# Safety boundary -- the handoff's hardest requirement
# ---------------------------------------------------------------------------

def test_no_authentication_code_exists():
    """The adapter must never act as the operator.

    Asserted by reading the source, because "we agreed not to" is not a
    control. A future edit adding a login should be hard to make by accident,
    and this is what makes it hard.
    """
    import dfs_pipeline.adapters.dk_api as module

    source = Path(module.__file__).read_text().lower()
    # Strip the docstring, which legitimately discusses what is forbidden.
    body = source.split('"""', 2)[-1]

    forbidden = [
        "requests.post", "requests.put", "requests.delete", "requests.patch",
        "requests.session", "cookiejar", "set_cookie",
        "password", "auth=", "authorization", "bearer",
    ]
    for term in forbidden:
        assert term not in body, f"forbidden construct in dk_api.py: {term!r}"


def test_only_get_requests_are_issued(monkeypatch, adapter):
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None, allow_redirects=None):
        calls.append({"url": url, "headers": headers or {}})

        class R:
            status_code = 200
            content = DRAFTABLES.read_bytes()

            @staticmethod
            def json():
                return json.loads(R.content)

        return R()

    monkeypatch.setattr("dfs_pipeline.adapters.dk_api.requests.get", fake_get)
    adapter.raw_bytes()
    assert len(calls) == 1
    assert "cookie" not in {k.lower() for k in calls[0]["headers"]}
    assert "authorization" not in {k.lower() for k in calls[0]["headers"]}


def test_user_agent_identifies_the_tool_honestly(monkeypatch, adapter):
    """Not a spoofed browser string."""
    from dfs_pipeline.adapters.dk_api import USER_AGENT

    assert "nfl-dfs-pipeline" in USER_AGENT
    for browser in ("Mozilla", "Chrome", "Safari", "AppleWebKit"):
        assert browser not in USER_AGENT


def test_failures_point_at_the_csv_fallback(monkeypatch, adapter):
    """The manual path is a first-class equal, not a degraded mode."""
    import requests as requests_module

    def boom(*_a, **_k):
        raise requests_module.ConnectionError("refused")

    monkeypatch.setattr("dfs_pipeline.adapters.dk_api.requests.get", boom)
    with pytest.raises(DraftKingsApiError, match="DKSalaries.csv"):
        adapter.raw_bytes()


def test_non_200_mentions_that_endpoints_are_undocumented(monkeypatch, adapter):
    class R:
        status_code = 404
        content = b""

    monkeypatch.setattr("dfs_pipeline.adapters.dk_api.requests.get", lambda *a, **k: R())
    with pytest.raises(DraftKingsApiError, match="undocumented"):
        adapter.raw_bytes()


# ---------------------------------------------------------------------------
# The FLEX duplication trap
# ---------------------------------------------------------------------------

def test_the_fixture_actually_contains_the_duplication():
    """Guards the premise. Without duplicate rows the next test proves nothing."""
    entries = json.loads(DRAFTABLES.read_text())["draftables"]
    per_player = {}
    for e in entries:
        per_player.setdefault(e["playerDkId"], []).append(e)
    assert len(entries) == 44
    assert len(per_player) == 27
    assert sum(1 for rows in per_player.values() if len(rows) > 1) == 17


def test_rows_collapse_to_one_record_per_player(api_players):
    """44 rows, 27 players. Treating rows as players would invent 17 phantoms.

    Worse than phantom entries: the same person could appear twice in one
    lineup under two draftableIds, which is contest-illegal and invisible to
    every constraint check, because the solver sees two distinct ids.
    """
    assert len(api_players) == 27
    assert len({p.source_player_id for p in api_players}) == 27
    assert len({p.stable_player_id for p in api_players}) == 27


def test_the_position_slot_id_wins_over_flex(api_players):
    """The CSV carries the position-slot draftableId, never the FLEX one.

    If the FLEX id were chosen instead, every source id would disagree with
    the CSV and the two paths could never be reconciled.
    """
    entries = json.loads(DRAFTABLES.read_text())["draftables"]
    flex_ids = {
        str(e["draftableId"]) for e in entries if e["rosterSlotId"] == FLEX_SLOT_ID
    }
    assert flex_ids, "fixture has no FLEX rows"
    assert not ({p.source_player_id for p in api_players} & flex_ids)


def test_roster_slots_are_collected_across_rows(api_players):
    flex_eligible = [p for p in api_players if "FLEX" in p.roster_positions]
    assert flex_eligible
    for player in flex_eligible:
        assert len(player.roster_positions) >= 2
        assert set(player.roster_positions) <= set(ROSTER_SLOTS.values())


def test_roster_slot_map_matches_the_positions_it_claims():
    assert ROSTER_SLOTS[FLEX_SLOT_ID] == "FLEX"
    assert set(ROSTER_SLOTS.values()) == {"QB", "RB", "WR", "TE", "FLEX", "DST"}


def test_missing_stable_id_is_fatal_not_guessed():
    """Without playerDkId the duplicates cannot be collapsed safely."""
    payload = json.dumps({"draftables": [{"draftableId": 1, "displayName": "X"}]}).encode()
    with pytest.raises(SlateSchemaError, match="playerDkId"):
        DraftKingsApiAdapter().loads(payload)


# ---------------------------------------------------------------------------
# Golden equivalence: API and CSV must agree
# ---------------------------------------------------------------------------

def test_both_paths_return_the_same_players(api_players, csv_players):
    assert len(api_players) == len(csv_players) == 27
    assert ({p.source_player_id for p in api_players}
            == {p.source_player_id for p in csv_players})


def test_every_shared_field_agrees(api_players, csv_players):
    """The handoff's golden requirement, on real captures of the same slate.

    For every field both paths can express, the normalized records must be
    semantically identical. Downstream code must not be able to tell which
    source produced a slate.
    """
    api = {p.source_player_id: p for p in api_players}
    for csv_player in csv_players:
        other = api[csv_player.source_player_id]
        assert other.name == csv_player.name
        assert other.position == csv_player.position
        assert other.salary == csv_player.salary
        assert other.team == csv_player.team
        assert other.game.key == csv_player.game.key
        assert other.entity_type == csv_player.entity_type
        assert set(other.roster_positions) == set(csv_player.roster_positions)


def test_kickoff_times_agree_across_paths(api_players, csv_players):
    """The CSV parses '01:00PM ET' through a DST-aware conversion; the API
    states UTC directly. They must land on the same instant."""
    api = {p.source_player_id: p for p in api_players}
    for csv_player in csv_players:
        assert api[csv_player.source_player_id].game.kickoff_utc == (
            csv_player.game.kickoff_utc
        )


def test_api_only_fields_are_populated_only_on_the_api_path(api_players, csv_players):
    """API-only metadata may enrich the schema; it must not be invented."""
    assert all(p.draft_group_id == 151307 for p in api_players)
    assert all(p.lock_time_utc is not None for p in api_players)
    assert all(p.stable_player_id is not None for p in api_players)

    assert all(p.draft_group_id is None for p in csv_players)
    assert all(p.lock_time_utc is None for p in csv_players)
    assert all(p.stable_player_id is None for p in csv_players)


def test_the_stable_id_differs_from_the_slate_id(api_players):
    """draftableId is reissued every slate; playerDkId is not.

    This is what a persistent crosswalk should key on -- a resolution made in
    Week 3 stays valid in Week 12 by id rather than by name.
    """
    for player in api_players:
        assert player.stable_player_id != player.source_player_id


def test_status_agrees_where_both_paths_report_one(api_players, csv_players):
    api = {p.source_player_id: p for p in api_players}
    compared = 0
    for csv_player in csv_players:
        if csv_player.status:
            assert api[csv_player.source_player_id].status == csv_player.status
            compared += 1
    assert compared > 0, "fixture should contain flagged players"


def test_clear_players_have_no_status_on_either_path(api_players):
    """DraftKings writes the string 'None' for a clear player; that is not a
    status, and storing it as one would create a phantom designation."""
    assert any(p.status is None for p in api_players)
    assert "NONE" not in {p.status for p in api_players if p.status}


# ---------------------------------------------------------------------------
# Draft group discovery
# ---------------------------------------------------------------------------

def _group(**kwargs) -> DraftGroup:
    base = dict(draft_group_id=1, game_count=12, start_time="2026-09-13T17:00:00Z",
                suffix="", tag="Featured", game_type_id=CLASSIC_GAME_TYPE_ID)
    return DraftGroup(**{**base, **kwargs})


def test_simulated_contests_are_identified():
    """DraftKings runs Madden Stream contests with real salaries. Not football."""
    assert _group(suffix=" (Madden Stream CIN @ DET)").is_simulated
    assert not _group(suffix=" (Preseason)").is_simulated


def test_single_game_groups_are_not_classic_candidates():
    assert not _group(game_count=1).is_classic_candidate
    assert _group(game_count=12).is_classic_candidate


def test_simulated_groups_are_never_classic_candidates():
    assert not _group(game_count=3, suffix="(Madden Stream)").is_classic_candidate


def test_main_slate_ignores_non_classic_formats(monkeypatch, adapter):
    """Regression: a 16-game Sit & Go was auto-selected over the real slate.

    Sit & Go is a SNAKE DRAFT. Its draftables carry no salaries at all -- the
    live run returned 4,501 entries with no salary field. The old heuristic
    ("largest non-simulated multi-game slate") inferred a structural property
    that DraftKings states outright in GameTypeId.
    """
    monkeypatch.setattr(
        adapter, "discover_draft_groups",
        lambda: [
            _group(draft_group_id=10, game_count=3, game_type_id=158,
                   suffix="(Madden Stream)"),
            _group(draft_group_id=20, game_count=1, game_type_id=96,
                   suffix="(NE @ SEA)"),
            _group(draft_group_id=146163, game_count=16, game_type_id=145,
                   suffix="(Sit & Go)"),
            _group(draft_group_id=151307, game_count=12),
        ],
    )
    assert adapter.find_main_slate().draft_group_id == 151307


def test_only_game_type_one_is_classic():
    assert _group(game_type_id=1).is_classic
    for other in (96, 145, 158, 159, None):
        assert not _group(game_type_id=other).is_classic


def test_no_classic_slate_fails_clearly(monkeypatch, adapter):
    monkeypatch.setattr(
        adapter, "discover_draft_groups",
        lambda: [_group(game_count=16, game_type_id=145, suffix="(Sit & Go)")],
    )
    with pytest.raises(DraftKingsApiError, match="salary-cap Classic"):
        adapter.find_main_slate()


def test_lobby_without_draft_groups_is_rejected(monkeypatch, adapter):
    class R:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            return {"Contests": []}

    monkeypatch.setattr("dfs_pipeline.adapters.dk_api.requests.get", lambda *a, **k: R())
    with pytest.raises(SlateSchemaError, match="no DraftGroups"):
        adapter.discover_draft_groups()


def test_discovery_parses_a_real_shaped_lobby_payload(monkeypatch, adapter):
    payload = {"DraftGroups": [
        {"DraftGroupId": 151307, "GameCount": 12, "StartDate": "2026-09-13T17:00:00Z",
         "ContestStartTimeSuffix": None, "DraftGroupTag": "Featured", "GameTypeId": 1},
        {"DraftGroupId": 152215, "GameCount": 1, "StartDate": "2026-08-17T18:00:00Z",
         "ContestStartTimeSuffix": " (Madden Stream CIN @ DET)",
         "DraftGroupTag": "Featured", "GameTypeId": 159},
    ]}

    class R:
        status_code = 200
        content = json.dumps(payload).encode()

        @staticmethod
        def json():
            return payload

    monkeypatch.setattr("dfs_pipeline.adapters.dk_api.requests.get", lambda *a, **k: R())
    groups = adapter.discover_draft_groups()
    assert [g.draft_group_id for g in groups] == [151307, 152215]
    assert groups[0].game_type_id == 1 and groups[0].is_classic
    assert groups[1].is_simulated and not groups[1].is_classic


# ---------------------------------------------------------------------------
# Malformed responses
# ---------------------------------------------------------------------------

def test_non_json_is_rejected(adapter):
    with pytest.raises(SlateSchemaError, match="not valid JSON"):
        adapter.loads(b"<html>502 Bad Gateway</html>")


def test_missing_draftables_key_points_at_the_csv_path(adapter):
    with pytest.raises(SlateSchemaError, match="DKSalaries.csv"):
        adapter.loads(b'{"errorStatus": "something"}')


def test_empty_draft_group_is_rejected(adapter):
    with pytest.raises(SlateSchemaError, match="no players"):
        adapter.loads(b'{"draftables": []}')


def test_single_game_slate_is_rejected(adapter):
    entries = json.loads(DRAFTABLES.read_text())["draftables"]
    one_game = [e for e in entries
                if e["competition"]["name"] == entries[0]["competition"]["name"]]
    with pytest.raises(SlateSchemaError, match="Showdown"):
        adapter.loads(json.dumps({"draftables": one_game}).encode())


def test_unparseable_competition_name_is_rejected(adapter):
    entry = json.loads(DRAFTABLES.read_text())["draftables"][0]
    entry["competition"] = {"name": "POSTPONED", "startTime": None}
    with pytest.raises(SlateSchemaError, match="could not read a matchup"):
        adapter.loads(json.dumps({"draftables": [entry]}).encode())


def test_unknown_team_is_rejected(adapter):
    entry = json.loads(DRAFTABLES.read_text())["draftables"][0]
    entry["teamAbbreviation"] = "XXX"
    with pytest.raises(SlateSchemaError, match="unknown NFL team"):
        adapter.loads(json.dumps({"draftables": [entry]}).encode())


def test_missing_salary_names_the_likely_cause(adapter):
    """A draft group with no salaries is a draft-style contest, not corruption.

    Verified live: draft group 146163 (Sit & Go) returned 4,501 draftables
    with no salary field. Naming that turns a confusing parse error into an
    actionable one.
    """
    entry = json.loads(DRAFTABLES.read_text())["draftables"][0]
    entry.pop("salary")
    with pytest.raises(SlateSchemaError, match="Sit & Go"):
        adapter.loads(json.dumps({"draftables": [entry]}).encode())


def test_entry_missing_a_name_is_rejected(adapter):
    entry = json.loads(DRAFTABLES.read_text())["draftables"][0]
    entry["displayName"] = ""
    with pytest.raises(SlateSchemaError, match="missing name or position"):
        adapter.loads(json.dumps({"draftables": [entry]}).encode())


# ---------------------------------------------------------------------------
# Timestamp handling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-09-13T17:00:00.0000000Z", "2026-09-13T17:00:00Z"),
        ("2026-09-13T17:00:00Z", "2026-09-13T17:00:00Z"),
        ("2026-09-13T17:00:00", "2026-09-13T17:00:00Z"),
        ("2026-09-13T13:00:00-04:00", "2026-09-13T17:00:00Z"),
    ],
)
def test_draftkings_seven_digit_fractional_seconds_parse(raw, expected):
    """DraftKings writes 7-digit fractions, which fromisoformat rejects."""
    assert _utc(raw) == expected


def test_unparseable_timestamp_becomes_none_not_a_guess():
    assert _utc("not a time") is None
    assert _utc(None) is None
    assert _utc("") is None


# ---------------------------------------------------------------------------
# Capture, and interchangeability with the CSV path
# ---------------------------------------------------------------------------

def test_api_slate_captures_into_the_store(store, offline):
    result = ingest_slate(store, offline, captured_at="2026-09-11T18:00:00Z")
    assert result.total_entries == 27
    assert result.games >= 2
    salaries = store.as_of("2026-09-12T00:00:00Z", metric="dk_salary")
    assert len(salaries) == 27


def test_the_stable_id_reaches_the_store(store, offline):
    ingest_slate(store, offline, captured_at="2026-09-11T18:00:00Z")
    stable = store.as_of("2026-09-12T00:00:00Z", metric="dk_stable_player_id")
    assert len(stable) == 27


def test_lock_times_reach_the_store(store, offline):
    ingest_slate(store, offline, captured_at="2026-09-11T18:00:00Z")
    locks = store.as_of("2026-09-12T00:00:00Z", metric="dk_lock_time")
    assert len(locks) == 27
    assert all(o.value.endswith("Z") for o in locks)


def test_both_paths_record_identical_salaries(tmp_path, offline):
    """Interchangeability, proven through the store rather than in memory."""
    def salaries_from(source, path):
        with SnapshotStore.open(path) as store:
            ingest_slate(store, source, captured_at="2026-09-11T18:00:00Z")
            return {
                o.source_subject_id: o.value
                for o in store.as_of("2026-09-12T00:00:00Z", metric="dk_salary")
            }

    from_api = salaries_from(offline, tmp_path / "api.sqlite")
    from_csv = salaries_from(DraftKingsCsvAdapter(SALARIES), tmp_path / "csv.sqlite")
    assert from_api == from_csv


def test_the_suite_never_reaches_draftkings(monkeypatch, offline, store):
    """Regression: four capture tests once hit the live endpoint.

    `ingest_slate` calls `raw_bytes()`, which on the real adapter fetches. A
    suite that reaches DraftKings fails offline, hammers someone else's
    servers, and silently changes behaviour week to week as slates roll over.
    """
    def forbidden(*_a, **_k):
        raise AssertionError("a test attempted a live DraftKings request")

    monkeypatch.setattr("dfs_pipeline.adapters.dk_api.requests.get", forbidden)
    result = ingest_slate(store, offline, captured_at="2026-09-11T18:00:00Z")
    assert result.total_entries == 27


def test_auto_selection_happens_when_no_draft_group_is_given(monkeypatch):
    """Discovery runs only when the operator did not name a slate."""
    adapter = DraftKingsApiAdapter()
    monkeypatch.setattr(
        adapter, "find_main_slate",
        lambda: DraftGroup(draft_group_id=151307, game_count=12,
                           start_time="2026-09-13T17:00:00Z", suffix="", tag=""),
    )

    class R:
        status_code = 200
        content = DRAFTABLES.read_bytes()

    monkeypatch.setattr("dfs_pipeline.adapters.dk_api.requests.get", lambda *a, **k: R())
    adapter.raw_bytes()
    assert adapter.draft_group_id == 151307, "selected group should be remembered"


def test_avg_points_comes_from_draft_stat_attribute_90(api_players):
    """DraftKings buries AvgPointsPerGame in a list of stat attributes."""
    with_avg = [p for p in api_players if p.avg_points_per_game is not None]
    assert with_avg
    assert all(p.avg_points_per_game >= 0 for p in with_avg)


def test_malformed_stat_attribute_yields_none_not_zero(adapter):
    """Absent is not zero, and conflating them would corrupt a mean."""
    entries = json.loads(DRAFTABLES.read_text())["draftables"]
    for entry in entries:
        entry["draftStatAttributes"] = [{"id": 90, "value": "not-a-number"}]
    players = adapter.loads(json.dumps({"draftables": entries}).encode())
    assert all(p.avg_points_per_game is None for p in players)


def test_missing_stat_attributes_yields_none(adapter):
    entries = json.loads(DRAFTABLES.read_text())["draftables"]
    for entry in entries:
        entry.pop("draftStatAttributes", None)
    players = adapter.loads(json.dumps({"draftables": entries}).encode())
    assert all(p.avg_points_per_game is None for p in players)
