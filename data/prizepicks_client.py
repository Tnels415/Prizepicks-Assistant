from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import requests

from config import PROP_STAT_MAP, NBA_TEAM_NAME_TO_ABBR

logger = logging.getLogger(__name__)

PROPS_FILE = Path("props.json")

# The Odds API — free tier: 500 requests/month (sign up at the-odds-api.com)
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
ODDS_MARKETS = [
    "player_points",
    "player_rebounds",
    "player_assists",
    "player_threes",
    "player_steals",
    "player_blocks",
    "player_turnovers",
]
MARKET_TO_STAT = {
    "player_points":    "Points",
    "player_rebounds":  "Rebounds",
    "player_assists":   "Assists",
    "player_threes":    "3-PT Made",
    "player_steals":    "Steals",
    "player_blocks":    "Blocks",
    "player_turnovers": "Turnovers",
}
# Priority order — whichever is available first is used
PREFERRED_BOOKS = ["draftkings", "fanduel", "betmgm", "williamhill_us", "pointsbet_us", "bovada"]

PROPS_FILE_INSTRUCTIONS = """
=======================================================
  ACTION REQUIRED: Fill in today's player prop lines
=======================================================
Neither The Odds API nor the live API returned props.

Either:
  A) Add your Odds API key to .env:
       THE_ODDS_API_KEY=your_key_here
     Get a free key (500 req/month) at: the-odds-api.com

  B) Manually fill in props.json with today's lines,
     then run python3 main.py again.

props.json format:
  [
    {
      "player_name": "Jayson Tatum",
      "team_abbr": "BOS",
      "stat_type": "Points",
      "line": 27.5
    }
  ]

Supported stat_type values:
  Points, Rebounds, Assists, 3-PT Made, Steals, Blocks,
  Turnovers, Pts+Reb+Ast, Pts+Ast, Pts+Reb, Reb+Ast
=======================================================
"""

PROPS_TEMPLATE = [
    {"player_name": "Jayson Tatum",   "team_abbr": "BOS", "stat_type": "Points",    "line": 27.5},
    {"player_name": "Luka Doncic",    "team_abbr": "DAL", "stat_type": "Assists",   "line": 8.5},
    {"player_name": "Nikola Jokic",   "team_abbr": "DEN", "stat_type": "Rebounds",  "line": 12.5},
]


class PrizePicksClient:

    def __init__(self, odds_api_key: str | None = None):
        self._odds_api_key = odds_api_key

    def fetch_nba_props(self) -> list[dict]:
        # 1. Try The Odds API (automatic, free)
        if self._odds_api_key:
            props = self._fetch_from_odds_api()
            if props:
                return props

        # 2. Fall back to manual props.json
        return self._load_from_file()

    # ------------------------------------------------------------------
    # The Odds API
    # ------------------------------------------------------------------

    def _fetch_from_odds_api(self) -> list[dict]:
        try:
            events = self._get_todays_events()
        except requests.exceptions.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            if code == 401:
                logger.error(
                    "The Odds API: invalid API key (401) — "
                    "check THE_ODDS_API_KEY in your .env file"
                )
            else:
                logger.warning("The Odds API events request failed (HTTP %s): %s", code, exc)
            return []
        except Exception as exc:
            logger.warning("The Odds API events request failed: %s", exc)
            return []

        if not events:
            logger.warning(
                "The Odds API: no NBA games found for today (%s). "
                "This can happen early in the day — try running again after noon.",
                date.today(),
            )
            return []

        logger.info("The Odds API: found %d NBA game(s) today", len(events))

        all_props: list[dict] = []
        last_resp = None

        for event in events:
            home = event.get("home_team", "?")
            away = event.get("away_team", "?")
            try:
                resp, event_props = self._get_event_props(event)
                last_resp = resp
                logger.info("  %-25s vs %-25s → %d props", away, home, len(event_props))
                all_props.extend(event_props)
            except requests.exceptions.HTTPError as exc:
                code = exc.response.status_code if exc.response is not None else "?"
                if code == 422:
                    logger.warning(
                        "  %s vs %s — props not posted yet (HTTP 422). "
                        "Lines are usually available 1–3 hours before tip-off.",
                        away, home,
                    )
                else:
                    logger.warning("  %s vs %s — HTTP %s, skipping", away, home, code)
            except Exception as exc:
                logger.warning("  %s vs %s — error: %s", away, home, exc)

        remaining = (
            last_resp.headers.get("x-requests-remaining", "?") if last_resp else "?"
        )
        logger.info(
            "The Odds API: %d props fetched total  |  %s requests remaining this month",
            len(all_props), remaining,
        )
        return all_props

    @staticmethod
    def _parse_commence_time(iso_str: str) -> datetime | None:
        try:
            return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        except Exception:
            return None

    def _get_todays_events(self) -> list[dict]:
        resp = requests.get(
            f"{ODDS_API_BASE}/sports/basketball_nba/events",
            params={"apiKey": self._odds_api_key, "dateFormat": "iso"},
            timeout=15,
        )
        resp.raise_for_status()
        events = resp.json()

        # Use a rolling UTC window instead of a date string comparison.
        # NBA games tip off as late as 10 PM ET = 2 AM UTC (next calendar day),
        # so a naive date string match would miss late games entirely.
        now_utc = datetime.now(timezone.utc)
        window_start = now_utc - timedelta(hours=4)   # include in-progress games
        window_end   = now_utc + timedelta(hours=20)  # up to ~10 PM local same day

        return [
            e for e in events
            if (ct := self._parse_commence_time(e.get("commence_time", "")))
            and window_start <= ct <= window_end
        ]

    def _get_event_props(self, event: dict) -> tuple[requests.Response, list[dict]]:
        resp = requests.get(
            f"{ODDS_API_BASE}/sports/basketball_nba/events/{event['id']}/odds",
            params={
                "apiKey": self._odds_api_key,
                "regions": "us",
                "markets": ",".join(ODDS_MARKETS),
                "oddsFormat": "american",
            },
            timeout=20,
        )
        resp.raise_for_status()
        return resp, self._parse_event_odds(resp.json(), event)

    def _parse_event_odds(self, data: dict, event: dict) -> list[dict]:
        home_full = data.get("home_team", "")
        away_full = data.get("away_team", "")
        home_abbr = NBA_TEAM_NAME_TO_ABBR.get(home_full, "")
        away_abbr = NBA_TEAM_NAME_TO_ABBR.get(away_full, "")

        # Pick best available bookmaker
        book = self._pick_bookmaker(data.get("bookmakers", []))
        if not book:
            return []

        seen: dict[tuple, dict] = {}
        for market in book.get("markets", []):
            stat_type = MARKET_TO_STAT.get(market.get("key", ""))
            if not stat_type:
                continue
            for outcome in market.get("outcomes", []):
                if outcome.get("name") != "Over":
                    continue
                player_name = outcome.get("description", "").strip()
                point = outcome.get("point")
                if not player_name or point is None:
                    continue
                key = (player_name, stat_type)
                if key not in seen:
                    seen[key] = {
                        "projection_id": f"{event['id']}_{player_name}_{stat_type}",
                        "player_name": player_name,
                        # team_abbr left blank; prop_analyzer derives it from the game log
                        "team_abbr": "",
                        "event_home_abbr": home_abbr,
                        "event_away_abbr": away_abbr,
                        "position": "",
                        "stat_type": stat_type,
                        "line": float(point),
                        "start_time": event.get("commence_time", ""),
                    }
        return list(seen.values())

    @staticmethod
    def _pick_bookmaker(bookmakers: list) -> dict | None:
        book_map = {b["key"]: b for b in bookmakers}
        for key in PREFERRED_BOOKS:
            if key in book_map:
                return book_map[key]
        return bookmakers[0] if bookmakers else None

    # ------------------------------------------------------------------
    # Manual fallback — read from props.json
    # ------------------------------------------------------------------

    def _load_from_file(self) -> list[dict]:
        if not PROPS_FILE.exists():
            self._write_template()
            print(PROPS_FILE_INSTRUCTIONS)
            return []

        try:
            raw = json.loads(PROPS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.error("props.json is not valid JSON: %s", exc)
            return []

        if not isinstance(raw, list) or not raw:
            logger.error("props.json must be a non-empty JSON array.")
            return []

        if raw == PROPS_TEMPLATE:
            print(PROPS_FILE_INSTRUCTIONS.replace(
                "Neither The Odds API nor the live API returned props.",
                "props.json still contains example data — replace with today's lines.",
            ))
            return []

        props = []
        for i, entry in enumerate(raw):
            parsed = self._parse_file_entry(entry, i)
            if parsed:
                props.append(parsed)

        logger.info("Loaded %d props from props.json", len(props))
        return props

    def _parse_file_entry(self, entry: dict, idx: int) -> dict | None:
        for field in ("player_name", "stat_type", "line"):
            if field not in entry:
                logger.warning("props.json entry %d missing '%s' — skipping", idx, field)
                return None

        stat_type = entry["stat_type"]
        if stat_type not in PROP_STAT_MAP:
            logger.warning(
                "props.json entry %d: unknown stat_type '%s'. Valid: %s",
                idx, stat_type, ", ".join(PROP_STAT_MAP.keys()),
            )
            return None

        try:
            line = float(entry["line"])
        except (TypeError, ValueError):
            logger.warning("props.json entry %d: invalid line value — skipping", idx)
            return None

        return {
            "projection_id": str(idx),
            "player_name": str(entry["player_name"]).strip(),
            "team_abbr": str(entry.get("team_abbr", "")).upper().strip(),
            "event_home_abbr": "",
            "event_away_abbr": "",
            "position": str(entry.get("position", "")).strip(),
            "stat_type": stat_type,
            "line": line,
            "start_time": "",
        }

    @staticmethod
    def _write_template() -> None:
        PROPS_FILE.write_text(json.dumps(PROPS_TEMPLATE, indent=2), encoding="utf-8")
