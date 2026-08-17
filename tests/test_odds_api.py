"""Tests for The Odds API adapter, secrets handling, and odds capture.

No test here touches the network. Live calls cost credits from a 500/month
budget, and a suite that spends real quota is a suite people stop running.
The fixture is a recorded slice of an actual API response, so the parser is
still tested against reality rather than against an invented shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dfs_pipeline.adapters import OddsApiAdapter, OddsApiError, QuotaExhausted
from dfs_pipeline.adapters.base import SlateSchemaError
from dfs_pipeline.capture import ingest_odds
from dfs_pipeline.secrets import MissingSecret, load_dotenv_value, read_odds_api_key
from dfs_pipeline.store import SnapshotStore

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "odds_nfl_sample.json"
KEY = "0123456789abcdef0123456789abcdef"


@pytest.fixture()
def raw() -> bytes:
    return SAMPLE.read_bytes()


@pytest.fixture()
def adapter() -> OddsApiAdapter:
    return OddsApiAdapter(KEY)


@pytest.fixture()
def store(tmp_path):
    with SnapshotStore.open(tmp_path / "snapshots.sqlite") as s:
        yield s


class FakeResponse:
    def __init__(self, *, status=200, content=b"[]", headers=None, text=""):
        self.status_code = status
        self.content = content
        self.headers = headers or {}
        self.text = text or content.decode("utf-8", "replace")


class RecordingStub:
    """Stands in for requests.get, recording calls and replaying responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params or {}})
        return self.responses.pop(0)


# ---------------------------------------------------------------------------
# Parsing a real recorded response
# ---------------------------------------------------------------------------

def test_parses_every_team_in_every_game(adapter, raw):
    rows = adapter.loads(raw)
    assert len({r.event_id for r in rows}) == 3
    assert len({r.bookmaker for r in rows}) == 9
    # 3 games x 2 teams x 9 bookmakers
    assert len(rows) == 54


def test_game_key_matches_draftkings_convention(adapter, raw):
    """AWAY@HOME, so odds join to a slate's dk_game without translation."""
    assert {r.game_key for r in adapter.loads(raw)} == {"ATL@PIT", "NE@SEA", "SF@LAR"}


def test_full_team_names_are_resolved_to_abbreviations(adapter, raw):
    teams = {r.team for r in adapter.loads(raw)}
    assert teams == {"ATL", "PIT", "NE", "SEA", "SF", "LAR"}


def test_spread_is_from_each_teams_own_perspective(adapter, raw):
    dk = {r.team: r for r in adapter.loads(raw) if r.bookmaker == "draftkings"}
    assert dk["SEA"].spread == -3.5, "home favourite should be negative"
    assert dk["NE"].spread == 3.5, "road underdog should be positive"
    assert dk["SEA"].spread == -dk["NE"].spread


def test_both_teams_share_the_game_total(adapter, raw):
    dk = {r.team: r for r in adapter.loads(raw) if r.bookmaker == "draftkings"}
    assert dk["SEA"].game_total == dk["NE"].game_total == 44.5


def test_home_and_away_are_identified(adapter, raw):
    dk = {r.team: r for r in adapter.loads(raw) if r.bookmaker == "draftkings"}
    assert dk["SEA"].is_home is True
    assert dk["NE"].is_home is False
    assert dk["SEA"].opponent == "NE"
    assert dk["NE"].opponent == "SEA"


def test_implied_team_total_reconciles_to_the_game_total(adapter, raw):
    """(total/2) - (spread/2), and the two sides must sum back to the total."""
    dk = {r.team: r for r in adapter.loads(raw) if r.bookmaker == "draftkings"}
    home, away = dk["SEA"], dk["NE"]
    assert home.implied_team_total == pytest.approx(24.0)
    assert away.implied_team_total == pytest.approx(20.5)
    assert home.implied_team_total + away.implied_team_total == pytest.approx(44.5)


def test_implied_total_is_none_without_both_inputs():
    """Derived values must not be invented from partial data."""
    from dfs_pipeline.adapters.odds_api import TeamOdds

    partial = TeamOdds(
        event_id="e", team="KC", opponent="BUF", is_home=True, game_key="BUF@KC",
        commence_time="2026-09-13T17:00:00Z", bookmaker="draftkings",
        effective_at="2026-09-13T16:00:00Z", spread=-3.0, game_total=None,
    )
    assert partial.implied_team_total is None


def test_effective_at_comes_from_the_bookmaker_not_from_us(adapter, raw):
    """Odds state when they were current; we record that rather than assume.

    This is the case the bitemporal store exists for -- unlike the DK CSV,
    which has no timestamp of its own.
    """
    for row in adapter.loads(raw):
        assert row.effective_at.endswith("Z")
        assert row.effective_at.startswith("2026-")


def test_each_bookmaker_becomes_its_own_source(adapter, raw):
    sources = {r.source_name for r in adapter.loads(raw)}
    assert "ODDS_API:draftkings" in sources
    assert "ODDS_API:fanduel" in sources
    assert len(sources) == 9


def test_subject_id_is_unique_per_team_per_game(adapter, raw):
    """Keying on team alone would collide across a multi-game window."""
    rows = adapter.loads(raw)
    per_source = {}
    for row in rows:
        per_source.setdefault(row.source_name, []).append(row.subject_id)
    for source, ids in per_source.items():
        assert len(ids) == len(set(ids)), f"duplicate subject ids for {source}"


# ---------------------------------------------------------------------------
# Malformed responses
# ---------------------------------------------------------------------------

def test_a_bookmaker_may_offer_one_market_and_not_another(adapter, raw):
    """Real books are not uniform, and the parser must not pretend they are.

    In the recorded response mybookieag posted a total for SF@LAR but no
    spread. The row is still captured -- the total is real data -- but the
    derived implied team total is None rather than computed from a guess.
    A fixture written from imagination would not have contained this.
    """
    rows = adapter.loads(raw)
    assert len(rows) == 54
    assert sum(1 for r in rows if r.game_total is not None) == 54
    assert sum(1 for r in rows if r.spread is not None) == 52

    partial = [r for r in rows if r.spread is None]
    assert len(partial) == 2
    for row in partial:
        assert row.game_total is not None, "the total is still real data"
        assert row.implied_team_total is None, "must not invent a derived value"


def test_non_json_response_is_rejected(adapter):
    with pytest.raises(SlateSchemaError, match="not valid JSON"):
        adapter.loads(b"<html>503 Service Unavailable</html>")


def test_non_list_response_is_rejected(adapter):
    with pytest.raises(SlateSchemaError, match="expected a list"):
        adapter.loads(b'{"message": "unexpected"}')


def test_event_missing_a_required_field_is_rejected(adapter):
    payload = json.dumps([{
        "id": "x", "home_team": "Seattle Seahawks",
        "away_team": "New England Patriots",
    }]).encode()
    with pytest.raises(SlateSchemaError, match="commence_time"):
        adapter.loads(payload)


def test_unknown_team_in_a_response_is_rejected(adapter):
    payload = json.dumps([{
        "id": "x", "home_team": "Toronto Huskies", "away_team": "Seattle Seahawks",
        "commence_time": "2026-09-13T17:00:00Z", "bookmakers": [],
    }]).encode()
    with pytest.raises(SlateSchemaError, match="unknown NFL team"):
        adapter.loads(payload)


def test_empty_response_yields_no_rows(adapter):
    assert adapter.loads(b"[]") == []


# ---------------------------------------------------------------------------
# Quota: the design constraint
# ---------------------------------------------------------------------------

def test_credit_cost_is_regions_times_markets():
    """Cost is per region per market, NOT per call.

    This multiplier is how a month's quota disappears in an afternoon.
    """
    assert OddsApiAdapter(KEY, regions=("us",), markets=("spreads", "totals")).credit_cost == 2
    assert OddsApiAdapter(KEY, regions=("us",), markets=("spreads",)).credit_cost == 1
    assert OddsApiAdapter(KEY, regions=("us", "uk"), markets=("spreads", "totals")).credit_cost == 4


def test_quota_check_costs_nothing(monkeypatch, adapter):
    stub = RecordingStub(FakeResponse(headers={"x-requests-remaining": "498"}))
    monkeypatch.setattr("dfs_pipeline.adapters.odds_api.requests.get", stub)
    assert adapter.check_quota() == 498
    assert stub.calls[0]["url"].endswith("/sports/")


def test_call_is_refused_below_the_quota_floor(monkeypatch):
    """A scheduled job must not exhaust the budget before a live slate."""
    stub = RecordingStub(FakeResponse(headers={"x-requests-remaining": "26"}))
    monkeypatch.setattr("dfs_pipeline.adapters.odds_api.requests.get", stub)
    adapter = OddsApiAdapter(KEY, min_quota=25)
    with pytest.raises(QuotaExhausted, match="below the floor"):
        adapter.raw_bytes()
    assert len(stub.calls) == 1, "the paid call should never have been made"


def test_call_proceeds_when_affordable(monkeypatch, raw):
    stub = RecordingStub(
        FakeResponse(headers={"x-requests-remaining": "498"}),
        FakeResponse(content=raw, headers={"x-requests-remaining": "496"}),
    )
    monkeypatch.setattr("dfs_pipeline.adapters.odds_api.requests.get", stub)
    adapter = OddsApiAdapter(KEY, min_quota=25)
    assert adapter.raw_bytes() == raw
    assert adapter.last_quota_remaining == 496


def test_request_window_is_applied(monkeypatch, raw):
    """Without a window the API returns the whole season, mostly irrelevant."""
    stub = RecordingStub(
        FakeResponse(headers={"x-requests-remaining": "498"}),
        FakeResponse(content=raw, headers={"x-requests-remaining": "496"}),
    )
    monkeypatch.setattr("dfs_pipeline.adapters.odds_api.requests.get", stub)
    OddsApiAdapter(KEY, days_ahead=8).raw_bytes()
    params = stub.calls[1]["params"]
    assert "commenceTimeFrom" in params and "commenceTimeTo" in params
    assert params["markets"] == "spreads,totals"


# ---------------------------------------------------------------------------
# The key must never appear in output
# ---------------------------------------------------------------------------

def test_api_key_is_scrubbed_from_error_messages(monkeypatch):
    """Credentials leak through error messages far more often than source."""
    stub = RecordingStub(
        FakeResponse(status=500, text=f"upstream error for apiKey={KEY}")
    )
    monkeypatch.setattr("dfs_pipeline.adapters.odds_api.requests.get", stub)
    with pytest.raises(OddsApiError) as exc:
        OddsApiAdapter(KEY).check_quota()
    assert KEY not in str(exc.value)
    assert "<ODDS_API_KEY>" in str(exc.value)


def test_network_failure_is_scrubbed_too(monkeypatch):
    import requests as requests_module

    def boom(url, params=None, timeout=None):
        raise requests_module.ConnectionError(f"failed connecting with apiKey={KEY}")

    monkeypatch.setattr("dfs_pipeline.adapters.odds_api.requests.get", boom)
    with pytest.raises(OddsApiError) as exc:
        OddsApiAdapter(KEY).check_quota()
    assert KEY not in str(exc.value)


def test_rejected_key_reports_clearly(monkeypatch):
    stub = RecordingStub(FakeResponse(status=401, text="unauthorised"))
    monkeypatch.setattr("dfs_pipeline.adapters.odds_api.requests.get", stub)
    with pytest.raises(OddsApiError, match="rejected the key"):
        OddsApiAdapter(KEY).check_quota()


def test_empty_key_is_refused_before_any_call():
    with pytest.raises(OddsApiError, match="no API key"):
        OddsApiAdapter("   ")


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

def test_environment_beats_dotenv(monkeypatch, tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("ODDS_API_KEY=from_file\n")
    monkeypatch.setenv("ODDS_API_KEY", "from_env")
    assert read_odds_api_key(dotenv) == "from_env"


def test_dotenv_is_used_when_environment_is_unset(monkeypatch, tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("ODDS_API_KEY=from_file\n")
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    assert read_odds_api_key(dotenv) == "from_file"


@pytest.mark.parametrize(
    "line, expected",
    [
        ("ODDS_API_KEY=plain\n", "plain"),
        ('ODDS_API_KEY="quoted"\n', "quoted"),
        ("ODDS_API_KEY='single'\n", "single"),
        ("  ODDS_API_KEY = spaced \n", "spaced"),
        ("export ODDS_API_KEY=exported\n", "exported"),
        ("# ODDS_API_KEY=commented\nODDS_API_KEY=real\n", "real"),
    ],
)
def test_dotenv_parsing_tolerates_how_people_actually_write_it(
    monkeypatch, tmp_path, line, expected
):
    dotenv = tmp_path / ".env"
    dotenv.write_text(line)
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    assert read_odds_api_key(dotenv) == expected


def test_placeholder_counts_as_missing(monkeypatch, tmp_path):
    """An unfilled .env.example copy should say so, not produce a 401."""
    dotenv = tmp_path / ".env"
    dotenv.write_text("ODDS_API_KEY=replace_me\n")
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    with pytest.raises(MissingSecret, match="not set"):
        read_odds_api_key(dotenv)


def test_missing_secret_names_where_to_put_it(monkeypatch, tmp_path):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    with pytest.raises(MissingSecret) as exc:
        read_odds_api_key(tmp_path / "absent.env")
    assert ".env" in str(exc.value)


def test_dotenv_reader_does_not_mutate_the_environment(monkeypatch, tmp_path):
    """A secret injected into os.environ leaks into every subprocess after."""
    dotenv = tmp_path / ".env"
    dotenv.write_text("ODDS_API_KEY=secret_value\n")
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    load_dotenv_value("ODDS_API_KEY", dotenv)
    import os

    assert "ODDS_API_KEY" not in os.environ


def test_missing_dotenv_file_returns_none(tmp_path):
    assert load_dotenv_value("ODDS_API_KEY", tmp_path / "nope.env") is None


# ---------------------------------------------------------------------------
# Capture into the store
# ---------------------------------------------------------------------------

class StubOddsSource:
    """An adapter that replays the fixture without any network access."""

    source_name = "ODDS_API"

    def __init__(self, raw: bytes):
        self._raw = raw
        self.last_quota_remaining = 496

    def raw_bytes(self) -> bytes:
        return self._raw

    def loads(self, raw: bytes):
        return OddsApiAdapter(KEY).loads(raw)


def test_odds_capture_records_observations(store, raw):
    result = ingest_odds(store, StubOddsSource(raw), captured_at="2026-08-17T17:00:00Z")
    assert result.games == 3
    assert result.bookmakers == 9
    assert result.team_rows == 54
    assert result.observations == store.observation_count()
    assert result.quota_remaining == 496
    assert store.artifact_count() == 1


def test_captured_odds_are_queryable_as_of_a_cutoff(store, raw):
    ingest_odds(store, StubOddsSource(raw), captured_at="2026-08-17T17:00:00Z")

    spreads = store.as_of("2026-08-17T18:00:00Z", metric="spread")
    # 52, not 54: one bookmaker posted a total without a spread for one
    # game. See test_a_bookmaker_may_offer_one_market_and_not_another.
    assert len(spreads) == 52

    dk = [o for o in spreads if o.source == "ODDS_API:draftkings"]
    assert len(dk) == 6

    # Nothing knowable before we captured it.
    assert store.as_of("2026-08-17T12:00:00Z", metric="spread") == []


def test_implied_team_total_is_stored(store, raw):
    ingest_odds(store, StubOddsSource(raw), captured_at="2026-08-17T17:00:00Z")
    implied = store.as_of("2026-08-17T18:00:00Z", metric="implied_team_total")
    assert len(implied) == 52, "implied total requires BOTH spread and total"
    assert all(5.0 < o.value < 45.0 for o in implied), "implausible implied totals"


def test_odds_join_to_a_slate_by_game_key(store, raw):
    """The point of resolving teams: odds_game must match dk_game."""
    ingest_odds(store, StubOddsSource(raw), captured_at="2026-08-17T17:00:00Z")
    games = {o.value for o in store.as_of("2026-08-17T18:00:00Z", metric="odds_game")}
    assert games == {"ATL@PIT", "NE@SEA", "SF@LAR"}


def test_reingesting_identical_odds_is_refused_by_default(store, raw):
    ingest_odds(store, StubOddsSource(raw), captured_at="2026-08-17T17:00:00Z")
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        ingest_odds(store, StubOddsSource(raw), captured_at="2026-08-17T17:00:00Z")


def test_recapturing_later_preserves_both_snapshots(store, raw):
    """Line movement is the whole point: two captures, two rows of history."""
    ingest_odds(store, StubOddsSource(raw), captured_at="2026-08-17T17:00:00Z")
    first = store.observation_count()
    ingest_odds(store, StubOddsSource(raw), captured_at="2026-08-17T21:00:00Z")
    assert store.observation_count() == first * 2, "second capture lost"
    # The as-of query still resolves exactly one row per team per bookmaker.
    assert len(store.as_of("2026-08-17T22:00:00Z", metric="spread")) == 52


# ---------------------------------------------------------------------------
# Remaining API error surfaces
# ---------------------------------------------------------------------------

def test_missing_quota_headers_are_an_error(monkeypatch):
    """Without quota headers we cannot know what a call would cost."""
    stub = RecordingStub(FakeResponse(headers={}))
    monkeypatch.setattr("dfs_pipeline.adapters.odds_api.requests.get", stub)
    with pytest.raises(OddsApiError, match="quota headers missing"):
        OddsApiAdapter(KEY).check_quota()


def test_bad_request_parameters_are_reported(monkeypatch):
    stub = RecordingStub(
        FakeResponse(headers={"x-requests-remaining": "498"}),
        FakeResponse(status=422, text="INVALID_MARKET"),
    )
    monkeypatch.setattr("dfs_pipeline.adapters.odds_api.requests.get", stub)
    with pytest.raises(OddsApiError, match="rejected the request parameters"):
        OddsApiAdapter(KEY).raw_bytes()


def test_server_error_on_the_odds_call_is_reported(monkeypatch):
    stub = RecordingStub(
        FakeResponse(headers={"x-requests-remaining": "498"}),
        FakeResponse(status=503, text="upstream unavailable"),
    )
    monkeypatch.setattr("dfs_pipeline.adapters.odds_api.requests.get", stub)
    with pytest.raises(OddsApiError, match="HTTP 503"):
        OddsApiAdapter(KEY).raw_bytes()


def test_network_failure_on_the_odds_call_is_reported(monkeypatch):
    import requests as requests_module

    calls = {"n": 0}

    def flaky(url, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(headers={"x-requests-remaining": "498"})
        raise requests_module.Timeout(f"timed out with apiKey={KEY}")

    monkeypatch.setattr("dfs_pipeline.adapters.odds_api.requests.get", flaky)
    with pytest.raises(OddsApiError) as exc:
        OddsApiAdapter(KEY).raw_bytes()
    assert KEY not in str(exc.value)


def test_bookmaker_without_a_key_is_skipped(adapter):
    payload = json.dumps([{
        "id": "x", "home_team": "Seattle Seahawks",
        "away_team": "New England Patriots",
        "commence_time": "2026-09-13T17:00:00Z",
        "bookmakers": [{"title": "Nameless", "markets": []}],
    }]).encode()
    assert adapter.loads(payload) == []


def test_market_without_a_timestamp_is_skipped(adapter):
    """No effective_at means we cannot say when the line was current."""
    payload = json.dumps([{
        "id": "x", "home_team": "Seattle Seahawks",
        "away_team": "New England Patriots",
        "commence_time": "2026-09-13T17:00:00Z",
        "bookmakers": [{
            "key": "somebook",
            "markets": [{"key": "spreads", "outcomes": [
                {"name": "Seattle Seahawks", "point": -3.5}]}],
        }],
    }]).encode()
    assert adapter.loads(payload) == []


def test_unparseable_point_becomes_none_not_zero(adapter):
    """Zero is a meaningful spread; absent must not be coerced into it."""
    payload = json.dumps([{
        "id": "x", "home_team": "Seattle Seahawks",
        "away_team": "New England Patriots",
        "commence_time": "2026-09-13T17:00:00Z",
        "bookmakers": [{
            "key": "somebook", "last_update": "2026-09-13T16:00:00Z",
            "markets": [{
                "key": "spreads", "last_update": "2026-09-13T16:00:00Z",
                "outcomes": [
                    {"name": "Seattle Seahawks", "point": "not-a-number"},
                    {"name": "New England Patriots", "point": None},
                ],
            }],
        }],
    }]).encode()
    rows = adapter.loads(payload)
    assert len(rows) == 2
    assert all(r.spread is None for r in rows)


def test_totals_market_without_an_over_outcome_is_skipped(adapter):
    payload = json.dumps([{
        "id": "x", "home_team": "Seattle Seahawks",
        "away_team": "New England Patriots",
        "commence_time": "2026-09-13T17:00:00Z",
        "bookmakers": [{
            "key": "somebook", "last_update": "2026-09-13T16:00:00Z",
            "markets": [{
                "key": "totals", "last_update": "2026-09-13T16:00:00Z",
                "outcomes": [{"name": "Under", "point": 44.5}],
            }],
        }],
    }]).encode()
    assert adapter.loads(payload) == []


def test_outcome_naming_an_unknown_team_is_skipped_not_fatal(adapter):
    """A stray outcome (e.g. a draw line) must not abort a whole slate."""
    payload = json.dumps([{
        "id": "x", "home_team": "Seattle Seahawks",
        "away_team": "New England Patriots",
        "commence_time": "2026-09-13T17:00:00Z",
        "bookmakers": [{
            "key": "somebook", "last_update": "2026-09-13T16:00:00Z",
            "markets": [{
                "key": "spreads", "last_update": "2026-09-13T16:00:00Z",
                "outcomes": [
                    {"name": "Draw", "point": 0},
                    {"name": "Seattle Seahawks", "point": -3.5},
                ],
            }],
        }],
    }]).encode()
    rows = adapter.loads(payload)
    assert len(rows) == 1
    assert rows[0].team == "SEA"


def test_unreadable_dotenv_returns_none(tmp_path, monkeypatch):
    target = tmp_path / ".env"
    target.write_text("ODDS_API_KEY=x\n")
    target.chmod(0o000)
    try:
        assert load_dotenv_value("ODDS_API_KEY", target) is None
    finally:
        target.chmod(0o644)


def test_dotenv_without_the_requested_key_returns_none(tmp_path):
    target = tmp_path / ".env"
    target.write_text("SOMETHING_ELSE=1\n\n# a comment\n")
    assert load_dotenv_value("ODDS_API_KEY", target) is None
