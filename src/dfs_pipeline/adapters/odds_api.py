"""The Odds API adapter: betting spreads and totals, captured point-in-time.

Isolated behind one class for the same reason as the DraftKings adapter --
when the upstream shape changes, one module changes.

Quota is a first-class design constraint
----------------------------------------
The free tier allows 500 requests per month, and **cost is one credit per
region per market**, not per call. Requesting ``us`` with ``spreads,totals``
costs 2 credits, not 1. That multiplier is how a month's quota disappears in
an afternoon, so:

* the cost of a call is computed and logged *before* it is made,
* remaining quota is read from response headers and logged every time,
* the adapter refuses to run below a configurable floor, so a scheduled job
  cannot silently exhaust the budget and leave a live slate uncaptured.

Quota can be checked for free: the ``/v4/sports`` endpoint returns the same
quota headers and costs 0 credits.

Bitemporality earns its keep here
---------------------------------
Every market carries its own ``last_update`` from the bookmaker. That is a
genuine ``effective_at`` distinct from our ``captured_at``: a line last moved
at 16:40 and read by us at 17:05 is exactly the two-timestamp case the store
was built for. Unlike the DraftKings CSV -- which has no timestamp of its own
-- odds tell us when they were current, so we record what they say rather
than assuming.

Every bookmaker is kept, not reduced
------------------------------------
Each bookmaker is stored as its own source (``ODDS_API:draftkings``). Books
disagree, and that disagreement is signal -- it cannot be recovered from a
consensus computed at capture time and stored alone. Consensus is a modelling
decision, and modelling decisions belong downstream of capture.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from dfs_pipeline.adapters.base import SlateSchemaError
from dfs_pipeline.teams import UnknownTeam, resolve_team

__all__ = [
    "OddsApiAdapter",
    "TeamOdds",
    "OddsApiError",
    "QuotaExhausted",
    "SOURCE_PREFIX",
    "API_BASE",
]

log = logging.getLogger("dfs_pipeline.odds")

API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY = "americanfootball_nfl"
SOURCE_PREFIX = "ODDS_API"

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class OddsApiError(RuntimeError):
    """A call to The Odds API failed or returned something unusable."""


class QuotaExhausted(OddsApiError):
    """Refused to spend credits that would take the account below its floor."""


@dataclass(frozen=True, slots=True)
class TeamOdds:
    """One bookmaker's view of one team's side of one game."""

    event_id: str
    team: str                    #: canonical abbreviation
    opponent: str
    is_home: bool
    game_key: str                #: "AWAY@HOME", matching DraftKings' convention
    commence_time: str           #: kickoff, UTC
    bookmaker: str
    effective_at: str            #: the market's own last_update, UTC
    spread: float | None = None
    game_total: float | None = None

    @property
    def source_name(self) -> str:
        return f"{SOURCE_PREFIX}:{self.bookmaker}"

    @property
    def subject_id(self) -> str:
        """Unique per team-per-game.

        Keying on the team alone would collide the moment a capture window
        spans more than one of that team's games -- silently overwriting one
        week's line with another's.
        """
        return f"{self.event_id}:{self.team}"

    @property
    def implied_team_total(self) -> float | None:
        """``(total / 2) - (spread / 2)``.

        Computed on demand rather than stored: it is derived from two captured
        facts, and storing a derived value invites it drifting out of step
        with the inputs it came from.
        """
        if self.spread is None or self.game_total is None:
            return None
        return (self.game_total / 2.0) - (self.spread / 2.0)


class OddsApiAdapter:
    """Fetches and normalizes NFL spreads and totals from The Odds API."""

    source_name = SOURCE_PREFIX

    def __init__(
        self,
        api_key: str,
        *,
        regions: tuple[str, ...] = ("us",),
        markets: tuple[str, ...] = ("spreads", "totals"),
        days_ahead: int = 8,
        min_quota: int = 25,
        timeout: float = 30.0,
        now: datetime | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise OddsApiError(
                "no API key supplied. Set ODDS_API_KEY in .env (see .env.example)."
            )
        self._key = api_key.strip()
        self.regions = regions
        self.markets = markets
        self.days_ahead = days_ahead
        self.min_quota = min_quota
        self.timeout = timeout
        self._now = now or datetime.now(timezone.utc)
        self.last_quota_remaining: int | None = None

    # -- quota -------------------------------------------------------------

    @property
    def credit_cost(self) -> int:
        """Credits one odds call will consume: regions x markets."""
        return len(self.regions) * len(self.markets)

    def _scrub(self, text: str) -> str:
        """Remove the API key from anything destined for a log or exception.

        Credentials leak through error messages and debug logs far more often
        than through source code. A failed request that dumps its full URL
        puts the key in plaintext wherever that message lands.
        """
        return text.replace(self._key, "<ODDS_API_KEY>")

    def check_quota(self) -> int:
        """Return remaining credits. Costs nothing -- ``/sports`` is free."""
        try:
            response = requests.get(
                f"{API_BASE}/sports/",
                params={"apiKey": self._key},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise OddsApiError(
                f"could not reach The Odds API: {self._scrub(str(exc))}"
            ) from None

        if response.status_code == 401:
            raise OddsApiError(
                "The Odds API rejected the key (401). Check ODDS_API_KEY in .env."
            )
        if response.status_code != 200:
            raise OddsApiError(
                f"quota check failed with HTTP {response.status_code}: "
                f"{self._scrub(response.text)[:200]}"
            )

        remaining = response.headers.get("x-requests-remaining")
        if remaining is None:
            raise OddsApiError("quota headers missing from the response")
        self.last_quota_remaining = int(float(remaining))
        return self.last_quota_remaining

    # -- fetch -------------------------------------------------------------

    def raw_bytes(self) -> bytes:
        """Fetch the raw JSON, after confirming the call is affordable."""
        remaining = self.check_quota()
        cost = self.credit_cost
        log.info(
            "odds quota: %s remaining; this call costs %s "
            "(%s region(s) x %s market(s))",
            remaining, cost, len(self.regions), len(self.markets),
        )

        if remaining - cost < self.min_quota:
            raise QuotaExhausted(
                f"refusing to spend {cost} credit(s): {remaining} remaining would "
                f"fall below the floor of {self.min_quota}. Raise --min-quota to "
                f"override, or wait for the monthly reset."
            )

        window_end = self._now + timedelta(days=self.days_ahead)
        params = {
            "apiKey": self._key,
            "regions": ",".join(self.regions),
            "markets": ",".join(self.markets),
            "oddsFormat": "american",
            # Without a window the API returns the entire season -- 272 events
            # at the time of writing -- almost all of it irrelevant to a slate.
            "commenceTimeFrom": self._now.strftime(_TS_FORMAT),
            "commenceTimeTo": window_end.strftime(_TS_FORMAT),
        }

        try:
            response = requests.get(
                f"{API_BASE}/sports/{SPORT_KEY}/odds/",
                params=params,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise OddsApiError(
                f"odds request failed: {self._scrub(str(exc))}"
            ) from None

        if response.status_code == 422:
            raise OddsApiError(
                f"The Odds API rejected the request parameters (422): "
                f"{self._scrub(response.text)[:200]}"
            )
        if response.status_code != 200:
            raise OddsApiError(
                f"odds request failed with HTTP {response.status_code}: "
                f"{self._scrub(response.text)[:200]}"
            )

        after = response.headers.get("x-requests-remaining")
        if after is not None:
            self.last_quota_remaining = int(float(after))
            log.info("odds quota after call: %s remaining", self.last_quota_remaining)

        return response.content

    # -- parsing -----------------------------------------------------------

    def loads(self, raw: bytes) -> list[TeamOdds]:
        """Normalize a raw API response into per-team records.

        Takes bytes rather than a parsed object so that re-parsing a stored
        artifact months later goes through exactly the code that parsed it
        originally.
        """
        try:
            events = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SlateSchemaError("odds response", f"not valid JSON: {exc}") from None

        if not isinstance(events, list):
            raise SlateSchemaError(
                "odds response",
                f"expected a list of events, got {type(events).__name__}",
            )

        records: list[TeamOdds] = []
        for index, event in enumerate(events):
            records.extend(self._parse_event(event, index))
        return records

    def _parse_event(self, event: dict, index: int) -> list[TeamOdds]:
        for field in ("id", "home_team", "away_team", "commence_time"):
            if field not in event:
                raise SlateSchemaError(
                    "odds response", f"event {index} is missing {field!r}"
                )

        try:
            home = resolve_team(event["home_team"])
            away = resolve_team(event["away_team"])
        except UnknownTeam as exc:
            raise SlateSchemaError("odds response", f"event {index}: {exc}") from None

        game_key = f"{away.abbrev}@{home.abbrev}"
        by_team: dict[tuple[str, str], dict] = {}

        for bookmaker in event.get("bookmakers", []):
            book = bookmaker.get("key")
            if not book:
                continue
            for market in bookmaker.get("markets", []):
                effective = market.get("last_update") or bookmaker.get("last_update")
                if not effective:
                    continue

                if market.get("key") == "spreads":
                    for outcome in market.get("outcomes", []):
                        try:
                            team = resolve_team(outcome.get("name", ""))
                        except UnknownTeam:
                            continue  # e.g. a draw line on an unexpected market
                        slot = by_team.setdefault((book, team.abbrev), {})
                        slot["spread"] = _as_float(outcome.get("point"))
                        slot["effective_at"] = effective
                        slot["team"] = team

                elif market.get("key") == "totals":
                    # Over and Under carry the same point; either states the
                    # game total. Both teams in the game share it.
                    total = next(
                        (
                            _as_float(o.get("point"))
                            for o in market.get("outcomes", [])
                            if str(o.get("name", "")).lower() == "over"
                        ),
                        None,
                    )
                    if total is None:
                        continue
                    for team in (home, away):
                        slot = by_team.setdefault((book, team.abbrev), {})
                        slot["game_total"] = total
                        slot.setdefault("effective_at", effective)
                        slot["team"] = team

        records = []
        for (book, abbrev), values in by_team.items():
            team = values["team"]
            records.append(
                TeamOdds(
                    event_id=event["id"],
                    team=abbrev,
                    opponent=away.abbrev if team is home else home.abbrev,
                    is_home=team is home,
                    game_key=game_key,
                    commence_time=event["commence_time"],
                    bookmaker=book,
                    effective_at=values["effective_at"],
                    spread=values.get("spread"),
                    game_total=values.get("game_total"),
                )
            )
        return records


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
