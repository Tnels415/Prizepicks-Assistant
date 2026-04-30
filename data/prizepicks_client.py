from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import PRIZEPICKS_URL, PRIZEPICKS_LEAGUE_ID, PRIZEPICKS_PER_PAGE, PRIZEPICKS_HEADERS, PROP_STAT_MAP

logger = logging.getLogger(__name__)

PROPS_FILE = Path("props.json")

PROPS_FILE_INSTRUCTIONS = """
=======================================================
  ACTION REQUIRED: Fill in today's PrizePicks lines
=======================================================
The PrizePicks API is blocked in your environment.

A template file has been created at:  props.json

Steps:
  1. Open PrizePicks and find today's NBA player props
  2. Edit props.json and fill in each player's line
  3. Run  python3 main.py  again

Supported stat_type values:
  Points, Rebounds, Assists, 3-PT Made, Steals, Blocks,
  Turnovers, Pts+Reb+Ast, Pts+Ast, Pts+Reb, Reb+Ast

Supported team abbreviations (NBA standard):
  ATL BKN BOS CHA CHI CLE DAL DEN DET GSW HOU IND
  LAC LAL MEM MIA MIL MIN NOP NYK OKC ORL PHI PHX
  POR SAC SAS TOR UTA WAS

=======================================================
"""

PROPS_TEMPLATE = [
    {
        "player_name": "LeBron James",
        "team_abbr": "LAL",
        "position": "F",
        "stat_type": "Points",
        "line": 25.5,
    },
    {
        "player_name": "Stephen Curry",
        "team_abbr": "GSW",
        "position": "G",
        "stat_type": "3-PT Made",
        "line": 4.5,
    },
    {
        "player_name": "Nikola Jokic",
        "team_abbr": "DEN",
        "position": "C",
        "stat_type": "Rebounds",
        "line": 12.5,
    },
]


class PrizePicksClient:

    def fetch_nba_props(self) -> list[dict]:
        # Try the live API first
        props = self._try_api()
        if props:
            return props

        # Fall back to manual props.json
        return self._load_from_file()

    # ------------------------------------------------------------------
    # Live API (optional — often blocked by corporate/home proxies)
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=2, max=6),
        retry=retry_if_exception_type((requests.exceptions.ConnectionError,
                                       requests.exceptions.Timeout)),
        reraise=False,
    )
    def _fetch_raw(self) -> dict | None:
        params = {
            "league_id": PRIZEPICKS_LEAGUE_ID,
            "per_page": PRIZEPICKS_PER_PAGE,
        }
        resp = requests.get(
            PRIZEPICKS_URL,
            headers=PRIZEPICKS_HEADERS,
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def _try_api(self) -> list[dict]:
        try:
            data = self._fetch_raw()
            if not data:
                return []
            player_lookup = self._build_player_lookup(data.get("included", []))
            props = []
            for proj in data.get("data", []):
                parsed = self._parse_projection(proj, player_lookup)
                if parsed:
                    props.append(parsed)
            if props:
                logger.info("Fetched %d NBA props from PrizePicks API", len(props))
            return props
        except Exception as exc:
            logger.debug("PrizePicks API unavailable (%s) — falling back to props.json", exc)
            return []

    def _build_player_lookup(self, included: list) -> dict[str, dict]:
        lookup = {}
        for item in included:
            if item.get("type") == "new_player":
                lookup[item["id"]] = item.get("attributes", {})
        return lookup

    def _parse_projection(self, proj: dict, player_lookup: dict) -> dict | None:
        attrs = proj.get("attributes", {})
        if attrs.get("status") != "pre_game":
            return None
        if attrs.get("projection_type") != "standard":
            return None
        stat_type = attrs.get("stat_type", "")
        if stat_type not in PROP_STAT_MAP:
            return None
        relationships = proj.get("relationships", {})
        player_id = relationships.get("new_player", {}).get("data", {}).get("id")
        player_attrs = player_lookup.get(player_id, {})
        player_name = player_attrs.get("name") or attrs.get("description", "")
        if not player_name:
            return None
        return {
            "projection_id": proj.get("id"),
            "player_name": player_name,
            "team_abbr": player_attrs.get("team", "").upper(),
            "position": player_attrs.get("position", ""),
            "stat_type": stat_type,
            "line": float(attrs.get("line_score", 0)),
            "start_time": attrs.get("start_time", ""),
        }

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

        # Detect if user left the template unchanged
        if raw == PROPS_TEMPLATE:
            print(PROPS_FILE_INSTRUCTIONS.replace(
                "Fill in today's PrizePicks lines",
                "props.json still contains example data — please replace with today's lines",
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
        required = ("player_name", "team_abbr", "stat_type", "line")
        for field in required:
            if field not in entry:
                logger.warning("props.json entry %d missing '%s' — skipping", idx, field)
                return None

        stat_type = entry["stat_type"]
        if stat_type not in PROP_STAT_MAP:
            logger.warning(
                "props.json entry %d: unknown stat_type '%s' — skipping. "
                "Valid types: %s",
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
            "team_abbr": str(entry["team_abbr"]).upper().strip(),
            "position": str(entry.get("position", "")).strip(),
            "stat_type": stat_type,
            "line": line,
            "start_time": "",
        }

    @staticmethod
    def _write_template() -> None:
        PROPS_FILE.write_text(
            json.dumps(PROPS_TEMPLATE, indent=2),
            encoding="utf-8",
        )
        logger.info("Created props.json template at %s", PROPS_FILE.resolve())
