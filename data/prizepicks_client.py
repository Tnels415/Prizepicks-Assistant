from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import requests

from config import SPORT_CONFIG, PRIZEPICKS_URL

logger = logging.getLogger(__name__)

PROPS_FILE = Path("props.json")

# The Odds API — free tier: 500 requests/month (sign up at the-odds-api.com)
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Priority bookmaker order
PREFERRED_BOOKS = ["draftkings", "fanduel", "betmgm", "williamhill_us", "pointsbet_us", "bovada"]


_PP_HEADER_VARIANTS = [
    # Variant 1: Desktop Chrome (original)
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Referer": "https://app.prizepicks.com/",
        "Origin": "https://app.prizepicks.com",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    },
    # Variant 2: Safari macOS
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.4.1 Safari/605.1.15"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": "https://app.prizepicks.com/",
        "Origin": "https://app.prizepicks.com",
    },
    # Variant 3: Mobile Chrome (Android)
    {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.6367.82 Mobile Safari/537.36"
        ),
        "Accept": "application/json",
        "Referer": "https://app.prizepicks.com/",
        "Origin": "https://app.prizepicks.com",
        "X-Device-Id": "prizepicks-client",
    },
]


class PrizePicksLiveClient:
    """
    Fetches live player prop lines directly from the PrizePicks projections API.
    No API key required — used as a free fallback when The Odds API is exhausted.
    Tries multiple header strategies in case Cloudflare bot detection blocks one.
    """

    def fetch_props(self, sport_config: dict) -> list[dict]:
        league_id = sport_config.get("prizepicks_league_id")
        if not league_id:
            return []

        sport_name = sport_config["name"]
        params = {"league_id": league_id, "per_page": 250, "single_stat": "true"}

        for i, headers in enumerate(_PP_HEADER_VARIANTS):
            try:
                resp = requests.get(
                    PRIZEPICKS_URL,
                    params=params,
                    headers=headers,
                    timeout=20,
                )
                if resp.status_code == 403:
                    logger.debug(
                        "PrizePicks API 403 with header variant %d for %s — trying next",
                        i + 1, sport_name,
                    )
                    continue
                resp.raise_for_status()
                data = resp.json()
                props = self._parse(data, sport_config)
                logger.info(
                    "PrizePicks API: %d %s props fetched (header variant %d)",
                    len(props), sport_name, i + 1,
                )
                return props
            except requests.exceptions.HTTPError:
                continue
            except Exception as exc:
                logger.warning("PrizePicks API error for %s (variant %d): %s", sport_name, i + 1, exc)
                break

        logger.warning(
            "PrizePicks API unavailable for %s (all %d header variants returned 403 or error). "
            "PrizePicks may have added bot protection. "
            "Use THE_ODDS_API_KEY in .env or fill props.json manually.",
            sport_name, len(_PP_HEADER_VARIANTS),
        )
        return []

    def _parse(self, data: dict, sport_config: dict) -> list[dict]:
        sport_name = sport_config["name"]
        prop_stat_map = sport_config["prop_stat_map"]
        pp_stat_map = sport_config.get("prizepicks_stat_map", {})
        team_name_to_abbr = sport_config.get("team_name_to_abbr", {})

        # Build player lookup: id → {name, team, position}
        players: dict[str, dict] = {}
        for obj in data.get("included", []):
            if obj.get("type") == "new_player":
                attrs = obj.get("attributes", {})
                raw_team = attrs.get("team", "")
                team_abbr = team_name_to_abbr.get(raw_team, raw_team)
                players[obj["id"]] = {
                    "name": attrs.get("name", ""),
                    "team": team_abbr,
                    "position": attrs.get("position", ""),
                }

        seen: dict[tuple, dict] = {}
        for proj in data.get("data", []):
            if proj.get("type") != "projection":
                continue
            attrs = proj.get("attributes", {})

            # Only include lines not yet started
            if attrs.get("status") not in ("pre_game", "pregame", None, ""):
                continue

            raw_stat = attrs.get("stat_type", "")
            # Normalize PrizePicks naming to our internal stat names
            stat_type = pp_stat_map.get(raw_stat, raw_stat)
            # None value in map means explicitly unsupported — skip
            if stat_type is None or stat_type not in prop_stat_map:
                continue

            line = attrs.get("line_score")
            if line is None:
                continue

            raw_rank = attrs.get("rank_type") or attrs.get("pick_type") or "standard"
            pick_type = str(raw_rank).lower().strip()

            player_id = (
                proj.get("relationships", {})
                    .get("new_player", {})
                    .get("data", {})
                    .get("id", "")
            )
            player = players.get(player_id, {})
            player_name = player.get("name", "")
            if not player_name:
                continue

            key = (player_name, stat_type)
            if key not in seen:
                seen[key] = {
                    "sport": sport_name,
                    "projection_id": proj.get("id", str(len(seen))),
                    "player_name": player_name,
                    "team_abbr": player.get("team", ""),
                    "event_home_abbr": "",
                    "event_away_abbr": "",
                    "position": player.get("position", ""),
                    "stat_type": stat_type,
                    "line": float(line),
                    "start_time": attrs.get("start_time", ""),
                    "pick_type": pick_type,
                }

        return list(seen.values())

PROPS_FILE_INSTRUCTIONS = """
=======================================================
  ACTION REQUIRED: No prop lines available today
=======================================================
PrizePicks API did not return props (may be temporarily
unavailable or blocking automated requests).
A props.json file has been created for you to fill in.

Fill props.json manually with today's PrizePicks lines,
then re-run: python3 main.py

props.json format (remove these examples first):
  [
    {"sport": "NBA", "player_name": "Player Name",
     "team_abbr": "BOS", "stat_type": "Points", "line": 27.5},
    {"sport": "NHL", "player_name": "Player Name",
     "team_abbr": "EDM", "stat_type": "Goals", "line": 0.5},
    {"sport": "MLB", "player_name": "Player Name",
     "team_abbr": "LAD", "stat_type": "Hits", "line": 1.5}
  ]

Optional flags (add to any entry):
  "goblin": true  — PrizePicks goblin line (artificially low); UNDER suppressed
  "demon":  true  — PrizePicks demon line  (artificially high); OVER  suppressed

NBA stat types:  Points, Rebounds, Assists, 3-PT Made, Steals,
                 Blocks, Turnovers, Pts+Reb+Ast, Pts+Ast, Pts+Reb
NHL stat types:  Points, Goals, Assists, Shots on Goal, Power Play Points
MLB stat types:  Hits, Home Runs, RBIs, Total Bases, Runs Scored,
                 Stolen Bases, Strikeouts
NFL stat types:  Passing Yards, Rushing Yards, Receiving Yards,
                 Receptions, Passing TDs
=======================================================
"""

PROPS_TEMPLATE = [
    {"sport": "NBA", "player_name": "Jayson Tatum",  "team_abbr": "BOS", "stat_type": "Points",   "line": 27.5},
    {"sport": "NHL", "player_name": "Connor McDavid", "team_abbr": "EDM", "stat_type": "Points",   "line": 0.5},
    {"sport": "MLB", "player_name": "Shohei Ohtani",  "team_abbr": "LAD", "stat_type": "Hits",     "line": 1.5},
]


class OddsAPIClient:
    """
    Fetches player prop lines from The Odds API for any supported sport.
    Falls back to a manual props.json file if the API is unavailable.
    """

    def __init__(self, odds_api_key: str | None = None) -> None:
        self._odds_api_key = odds_api_key

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def fetch_props(self, sport_config: dict) -> list[dict]:
        """Return list of prop dicts for the given sport.

        Lines are sourced exclusively from PrizePicks so they always match
        what is actually available to play. props.json is the manual fallback
        when the PrizePicks API is unreachable.
        """
        props = PrizePicksLiveClient().fetch_props(sport_config)
        if props:
            return props

        return self._load_from_file(sport_config)

    # ------------------------------------------------------------------
    # The Odds API
    # ------------------------------------------------------------------

    def _fetch_from_odds_api(self, sport_config: dict) -> list[dict]:
        sport_key = sport_config["odds_sport_key"]
        sport_name = sport_config["name"]

        try:
            events = self._get_todays_events(sport_key)
        except requests.exceptions.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            if code == 401:
                logger.error("The Odds API: invalid API key (401) — check THE_ODDS_API_KEY in .env")
            else:
                logger.warning("The Odds API events request failed (HTTP %s): %s", code, exc)
            return []
        except Exception as exc:
            logger.warning("The Odds API events request failed: %s", exc)
            return []

        if not events:
            logger.warning(
                "The Odds API: no %s games found for today. "
                "This can happen early in the day — try again after noon.",
                sport_name,
            )
            return []

        logger.info("The Odds API: found %d %s game(s) today", len(events), sport_name)

        all_props: list[dict] = []
        last_resp = None

        for event in events:
            home = event.get("home_team", "?")
            away = event.get("away_team", "?")
            try:
                resp, event_props = self._get_event_props(event, sport_config)
                last_resp = resp
                logger.info("  %-25s vs %-25s → %d props", away, home, len(event_props))
                all_props.extend(event_props)
            except requests.exceptions.HTTPError as exc:
                code = exc.response.status_code if exc.response is not None else "?"
                if code == 401:
                    try:
                        api_msg = exc.response.json().get("message", exc.response.text[:200])
                    except Exception:
                        api_msg = "(no detail)"
                    logger.error(
                        "The Odds API: 401 Unauthorized fetching %s player props — %s. "
                        "Your plan may not include player prop markets. "
                        "Verify at the-odds-api.com that your subscription covers player props, "
                        "or remove THE_ODDS_API_KEY from .env to skip directly to PrizePicks.",
                        sport_name, api_msg,
                    )
                    break  # auth error won't recover for other games
                elif code == 422:
                    # Batch request failed — try each market individually so one
                    # bad/unavailable market key doesn't block all the others.
                    recovered = self._get_event_props_individually(event, sport_config)
                    if recovered:
                        logger.info(
                            "  %-25s vs %-25s → %d props (individual market fallback)",
                            away, home, len(recovered),
                        )
                        all_props.extend(recovered)
                    else:
                        try:
                            detail = exc.response.json().get("message", exc.response.text[:120])
                        except Exception:
                            detail = "props not yet posted or market unavailable on your plan"
                        logger.warning(
                            "  %s vs %s — HTTP 422: %s", away, home, detail
                        )
                else:
                    try:
                        api_msg = exc.response.json().get("message", "") if exc.response is not None else ""
                    except Exception:
                        api_msg = ""
                    logger.warning(
                        "  %s vs %s — HTTP %s, skipping%s",
                        away, home, code, f": {api_msg}" if api_msg else "",
                    )
            except Exception as exc:
                logger.warning("  %s vs %s — error: %s", away, home, exc)

        remaining = last_resp.headers.get("x-requests-remaining", "?") if last_resp else "?"
        logger.info(
            "The Odds API (%s): %d props total  |  %s requests remaining this month",
            sport_name, len(all_props), remaining,
        )
        if not all_props and events:
            logger.warning(
                "The Odds API returned 0 %s player props across %d game(s). "
                "Possible causes: (1) props not yet posted — try running after noon; "
                "(2) player prop markets not available on your current Odds API plan.",
                sport_name, len(events),
            )
        return all_props

    def _get_todays_events(self, sport_key: str) -> list[dict]:
        resp = requests.get(
            f"{ODDS_API_BASE}/sports/{sport_key}/events",
            params={"apiKey": self._odds_api_key, "dateFormat": "iso"},
            timeout=15,
        )
        resp.raise_for_status()
        events = resp.json()

        now_utc = datetime.now(timezone.utc)
        window_start = now_utc - timedelta(hours=4)
        window_end = now_utc + timedelta(hours=20)

        return [
            e for e in events
            if (ct := self._parse_commence_time(e.get("commence_time", "")))
            and window_start <= ct <= window_end
        ]

    def _get_event_props(
        self,
        event: dict,
        sport_config: dict,
    ) -> tuple[requests.Response, list[dict]]:
        markets = sport_config["markets"]
        sport_key = sport_config["odds_sport_key"]
        resp = requests.get(
            f"{ODDS_API_BASE}/sports/{sport_key}/events/{event['id']}/odds",
            params={
                "apiKey": self._odds_api_key,
                "regions": "us",
                "markets": ",".join(markets),
                "oddsFormat": "american",
            },
            timeout=20,
        )
        resp.raise_for_status()
        return resp, self._parse_event_odds(resp.json(), event, sport_config)

    def _get_event_props_individually(
        self,
        event: dict,
        sport_config: dict,
    ) -> list[dict]:
        """Try each market separately and merge results. Used as 422 fallback."""
        combined: dict[tuple, dict] = {}
        for market_key in sport_config["markets"]:
            single_cfg = dict(sport_config, markets=[market_key])
            try:
                _, props = self._get_event_props(event, single_cfg)
                for p in props:
                    key = (p["player_name"], p["stat_type"])
                    combined[key] = p
            except requests.exceptions.HTTPError as exc:
                code = exc.response.status_code if exc.response is not None else "?"
                logger.debug(
                    "  Market '%s' returned HTTP %s for event %s — skipping",
                    market_key, code, event.get("id", "?"),
                )
            except Exception as exc:
                logger.debug("  Market '%s' error: %s", market_key, exc)
        return list(combined.values())

    def _parse_event_odds(
        self,
        data: dict,
        event: dict,
        sport_config: dict,
    ) -> list[dict]:
        team_name_map = sport_config.get("team_name_to_abbr", {})
        market_to_stat = sport_config["market_to_stat"]
        sport_name = sport_config["name"]

        home_full = data.get("home_team", "")
        away_full = data.get("away_team", "")
        home_abbr = team_name_map.get(home_full, home_full[:3].upper() if home_full else "")
        away_abbr = team_name_map.get(away_full, away_full[:3].upper() if away_full else "")

        book = self._pick_bookmaker(data.get("bookmakers", []))
        if not book:
            return []

        seen: dict[tuple, dict] = {}
        for market in book.get("markets", []):
            stat_type = market_to_stat.get(market.get("key", ""))
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
                        "sport": sport_name,
                        "projection_id": f"{event['id']}_{player_name}_{stat_type}",
                        "player_name": player_name,
                        "team_abbr": "",          # derived from game log in prop_analyzer
                        "event_home_abbr": home_abbr,
                        "event_away_abbr": away_abbr,
                        "position": "",
                        "stat_type": stat_type,
                        "line": float(point),
                        "start_time": event.get("commence_time", ""),
                        "pick_type": "standard",
                    }
        return list(seen.values())

    @staticmethod
    def _parse_commence_time(iso_str: str) -> datetime | None:
        try:
            return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        except Exception:
            return None

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

    def _load_from_file(self, sport_config: dict) -> list[dict]:
        sport_name = sport_config["name"]
        prop_stat_map = sport_config["prop_stat_map"]

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
            # Only load entries matching this sport
            entry_sport = str(entry.get("sport", "NBA")).upper()
            if entry_sport != sport_name:
                continue
            parsed = self._parse_file_entry(entry, i, prop_stat_map, sport_name)
            if parsed:
                props.append(parsed)

        logger.info("Loaded %d %s props from props.json", len(props), sport_name)
        return props

    def _parse_file_entry(
        self,
        entry: dict,
        idx: int,
        prop_stat_map: dict,
        sport_name: str,
    ) -> dict | None:
        for field in ("player_name", "stat_type", "line"):
            if field not in entry:
                logger.warning("props.json entry %d missing '%s' — skipping", idx, field)
                return None

        stat_type = entry["stat_type"]
        if stat_type not in prop_stat_map:
            logger.warning(
                "props.json entry %d: unknown stat_type '%s' for %s. Valid: %s",
                idx, stat_type, sport_name, ", ".join(prop_stat_map.keys()),
            )
            return None

        try:
            line = float(entry["line"])
        except (TypeError, ValueError):
            logger.warning("props.json entry %d: invalid line value — skipping", idx)
            return None

        if entry.get("goblin"):
            pick_type = "goblin"
        elif entry.get("demon"):
            pick_type = "demon"
        else:
            pick_type = "standard"

        return {
            "sport": sport_name,
            "projection_id": str(idx),
            "player_name": str(entry["player_name"]).strip(),
            "team_abbr": str(entry.get("team_abbr", "")).upper().strip(),
            "event_home_abbr": "",
            "event_away_abbr": "",
            "position": str(entry.get("position", "")).strip(),
            "stat_type": stat_type,
            "line": line,
            "start_time": "",
            "pick_type": pick_type,
        }

    @staticmethod
    def _write_template() -> None:
        PROPS_FILE.write_text(json.dumps(PROPS_TEMPLATE, indent=2), encoding="utf-8")


# Backward-compatible alias used by older code
class PrizePicksClient(OddsAPIClient):
    def fetch_nba_props(self) -> list[dict]:
        return self.fetch_props(SPORT_CONFIG["NBA"])
