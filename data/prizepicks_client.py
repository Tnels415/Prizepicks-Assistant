from __future__ import annotations

import logging
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import PRIZEPICKS_URL, PRIZEPICKS_LEAGUE_ID, PRIZEPICKS_PER_PAGE, PRIZEPICKS_HEADERS, PROP_STAT_MAP

logger = logging.getLogger(__name__)


class PrizePicksClient:

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
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
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def fetch_nba_props(self) -> list[dict]:
        try:
            data = self._fetch_raw()
        except Exception as exc:
            logger.warning("PrizePicks API fetch failed: %s", exc)
            return []

        if not data:
            return []

        player_lookup = self._build_player_lookup(data.get("included", []))
        props = []
        for proj in data.get("data", []):
            parsed = self._parse_projection(proj, player_lookup)
            if parsed:
                props.append(parsed)

        logger.info("Fetched %d NBA props from PrizePicks", len(props))
        return props

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
        player_rel = relationships.get("new_player", {}).get("data", {})
        player_id = player_rel.get("id")
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
