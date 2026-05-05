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

# All 32 current NHL franchises — used to build the player DB from rosters.
_NHL_TEAMS = [
    "ANA", "BOS", "BUF", "CGY", "CAR", "CHI", "COL", "CBJ",
    "DAL", "DET", "EDM", "FLA", "LAK", "MIN", "MTL", "NSH",
    "NJD", "NYI", "NYR", "OTT", "PHI", "PIT", "SJS", "SEA",
    "STL", "TBL", "TOR", "UTA", "VAN", "VGK", "WSH", "WPG",
]

# Module-level player cache: lower-case full name → player_id.
# Built once per process from team rosters; avoids the broken search endpoint.
_NHL_PLAYER_DB: dict[str, int] = {}
_NHL_DB_LOADED: bool = False


def _build_nhl_player_db() -> None:
    """Populate _NHL_PLAYER_DB from every team's current roster."""
    global _NHL_PLAYER_DB, _NHL_DB_LOADED
    _NHL_DB_LOADED = True  # set early so we don't retry on partial failure
    db: dict[str, int] = {}
    loaded = 0
    for abbrev in _NHL_TEAMS:
        try:
            resp = requests.get(
                f"{NHL_WEB_API}/roster/{abbrev}/current",
                timeout=10,
            )
            if resp.status_code != 200:
                logger.debug("NHL roster %s returned %d", abbrev, resp.status_code)
                continue
            data = resp.json()
            for group in ("forwards", "defensemen", "goalies"):
                for player in data.get(group, []):
                    pid = player.get("id")
                    first = player.get("firstName", {}).get("default", "")
                    last = player.get("lastName", {}).get("default", "")
                    if pid and (first or last):
                        db[f"{first} {last}".strip().lower()] = pid
            loaded += 1
            time.sleep(0.05)
        except Exception as exc:
            logger.debug("NHL roster fetch failed for %s: %s", abbrev, exc)
    _NHL_PLAYER_DB = db
    logger.info(
        "NHL player DB built from %d/%d team rosters — %d players indexed",
        loaded, len(_NHL_TEAMS), len(db),
    )


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

        player_id = self._lookup_player(player_name)
        self._player_id_cache[player_name] = player_id
        return player_id

    def _lookup_player(self, player_name: str) -> int | None:
        if not _NHL_DB_LOADED:
            _build_nhl_player_db()

        if not _NHL_PLAYER_DB:
            logger.warning("NHL player DB is empty — cannot find '%s'", player_name)
            return None

        name_lower = player_name.strip().lower()

        # 1. Exact full-name match
        if name_lower in _NHL_PLAYER_DB:
            return _NHL_PLAYER_DB[name_lower]

        # 2. First-name + last-name partial match (handles middle initials, etc.)
        parts = name_lower.split()
        if len(parts) >= 2:
            first, last = parts[0], parts[-1]
            candidates = {
                k: v for k, v in _NHL_PLAYER_DB.items()
                if k.endswith(last) and k.startswith(first)
            }
            if len(candidates) == 1:
                return next(iter(candidates.values()))
            if len(candidates) > 1:
                # Prefer the entry whose name length is closest to the query
                return min(candidates.items(), key=lambda kv: abs(len(kv[0]) - len(name_lower)))[1]

            # 3. Last-name-only fallback (risky but better than nothing)
            last_only = {k: v for k, v in _NHL_PLAYER_DB.items() if k.endswith(f" {last}")}
            if len(last_only) == 1:
                return next(iter(last_only.values()))

        logger.warning("NHL: player '%s' not found in roster DB", player_name)
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
