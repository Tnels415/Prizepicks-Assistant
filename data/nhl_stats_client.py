from __future__ import annotations

import logging
import time
from datetime import date

import pandas as pd
import requests

from config import get_nhl_season
from data.base_stats_client import BaseStatsClient

logger = logging.getLogger(__name__)

NHL_WEB_API = "https://api-web.nhle.com/v1"
NHL_SEARCH_API = "https://search.d3.nhle.com/api/v1"


class NHLStatsClient(BaseStatsClient):

    def __init__(self) -> None:
        self._player_id_cache: dict[str, int | None] = {}
        self._game_log_cache: dict[int, pd.DataFrame] = {}

    # -------------------------------------------------------------------------
    # Player ID lookup
    # -------------------------------------------------------------------------

    def find_player_id(self, player_name: str) -> int | None:
        if player_name in self._player_id_cache:
            return self._player_id_cache[player_name]

        player_id = self._search_player(player_name)
        self._player_id_cache[player_name] = player_id
        return player_id

    def _search_player(self, player_name: str) -> int | None:
        try:
            resp = requests.get(
                f"{NHL_SEARCH_API}/search",
                params={"q": player_name, "type": "player", "culture": "en-us", "limit": 10},
                timeout=10,
            )
            resp.raise_for_status()
            results = resp.json()
            if not results:
                logger.warning("NHL: no results for player '%s'", player_name)
                return None

            # Prefer exact name match
            name_lower = player_name.lower()
            for r in results:
                if r.get("name", "").lower() == name_lower:
                    return r.get("playerId")

            # Accept first result
            return results[0].get("playerId")
        except Exception as exc:
            logger.warning("NHL player search failed for '%s': %s", player_name, exc)
            return None

    # -------------------------------------------------------------------------
    # Game log
    # -------------------------------------------------------------------------

    def get_player_game_log(self, player_id: int) -> pd.DataFrame:
        if player_id in self._game_log_cache:
            return self._game_log_cache[player_id]

        season = get_nhl_season()
        rows: list[dict] = []

        for game_type in (2, 3):   # 2 = regular season, 3 = playoffs
            rows.extend(self._fetch_game_log(player_id, season, game_type))
            time.sleep(0.3)

        if not rows:
            df = pd.DataFrame()
        else:
            df = pd.DataFrame(rows)
            df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"])
            df = df.sort_values("GAME_DATE", ascending=False).reset_index(drop=True)

        self._game_log_cache[player_id] = df
        return df

    def _fetch_game_log(
        self,
        player_id: int,
        season: str,
        game_type: int,
    ) -> list[dict]:
        try:
            resp = requests.get(
                f"{NHL_WEB_API}/player/{player_id}/game-log/{season}/{game_type}",
                timeout=15,
            )
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.debug("NHL game log (type=%d) failed for player %d: %s", game_type, player_id, exc)
            return []

        rows = []
        for g in data.get("gameLog", []):
            team_abbr = g.get("teamAbbrev", "")
            opp_abbr = g.get("opponentAbbrev", "")
            is_home = g.get("homeRoadFlag", "R") == "H"
            vs_str = "vs." if is_home else "@"
            rows.append({
                "GAME_DATE": g.get("gameDate", ""),
                "MATCHUP": f"{team_abbr} {vs_str} {opp_abbr}",
                "location": "Home" if is_home else "Away",
                "opponent_abbr": opp_abbr,
                "G": float(g.get("goals", 0)),
                "A": float(g.get("assists", 0)),
                "PTS": float(g.get("points", 0)),
                "SOG": float(g.get("shots", 0)),
                "PPP": float(g.get("powerPlayPoints", 0)),
                "HITS": float(g.get("hits", 0)),
            })
        return rows

    # -------------------------------------------------------------------------
    # Team stats (not available → return empty; factors degrade gracefully)
    # -------------------------------------------------------------------------

    def get_team_advanced_stats(self) -> pd.DataFrame:
        return pd.DataFrame()

    # -------------------------------------------------------------------------
    # Outcome evaluation helper
    # -------------------------------------------------------------------------

    def get_game_stats_for_date(self, player_id: int, game_date: date) -> dict | None:
        try:
            df = self.get_player_game_log(player_id)
            if df.empty or "GAME_DATE" not in df.columns:
                return {"played": False}
            mask = df["GAME_DATE"].dt.date == game_date
            rows = df[mask]
            if rows.empty:
                return {"played": False}
            row = rows.iloc[0]
            stats = {
                col: float(row[col])
                for col in ("G", "A", "PTS", "SOG", "PPP", "HITS")
                if col in row.index
            }
            stats["played"] = True
            return stats
        except Exception as exc:
            logger.warning("NHL get_game_stats_for_date failed (player %d): %s", player_id, exc)
            return None
