from __future__ import annotations

import json
import logging
from pathlib import Path

import requests

from config import SPORT_CONFIG, PRIZEPICKS_URL

logger = logging.getLogger(__name__)

PROPS_FILE = Path("props.json")


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


class PropLineClient:
    """Fetches prop lines exclusively from PrizePicks, with props.json as manual fallback."""

    def fetch_props(self, sport_config: dict) -> list[dict]:
        props = PrizePicksLiveClient().fetch_props(sport_config)
        if props:
            return props

        return self._load_from_file(sport_config)

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
                "props.json still contains example data — replace with today's lines.",
                "props.json still contains example data — replace with today's lines.",
            ))
            return []

        props = []
        for i, entry in enumerate(raw):
            entry_sport = str(entry.get("sport", "NBA")).upper()
            if entry_sport != sport_name:
                continue
            parsed = self._parse_file_entry(entry, i, prop_stat_map, sport_name)
            if parsed:
                props.append(parsed)

        logger.warning(
            "PrizePicks API unavailable — loaded %d %s props from props.json. "
            "Lines in this file may not match current PrizePicks lines. "
            "Update props.json with today's PrizePicks lines before using these picks.",
            len(props), sport_name,
        )
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


# Backward-compatible aliases
OddsAPIClient = PropLineClient

class PrizePicksClient(PropLineClient):
    def fetch_nba_props(self) -> list[dict]:
        return self.fetch_props(SPORT_CONFIG["NBA"])
