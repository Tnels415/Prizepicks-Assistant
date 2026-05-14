from __future__ import annotations

import logging
import time
from datetime import date

import pandas as pd
import requests

from data.base_stats_client import BaseStatsClient
from data.game_log_cache import GameLogCache

logger = logging.getLogger(__name__)

# Module-level cache singleton shared across all MLBStatsClient instances.
_GAME_LOG_CACHE = GameLogCache()

MLB_API = "https://statsapi.mlb.com/api/v1"

# Module-level player cache: lower-case full name → player_id.
# Built once per process from the season-wide active roster endpoint;
# avoids the deprecated people/search endpoint (now returns 404).
_MLB_PLAYER_DB: dict[str, int] = {}
# team_id → abbreviation: MLB game log splits include abbreviation on the
# *opponent* object but NOT on the player's own *team* object, so we need
# a separate lookup to build correct MATCHUP strings.
_MLB_TEAM_DB: dict[int, str] = {}
_MLB_DB_LOADED: bool = False


def _build_mlb_player_db() -> None:
    """Fetch all active MLB players and team abbreviations in one pass."""
    global _MLB_PLAYER_DB, _MLB_TEAM_DB, _MLB_DB_LOADED
    _MLB_DB_LOADED = True
    year = date.today().year
    try:
        resp = requests.get(
            f"{MLB_API}/sports/1/players",
            params={"season": year, "gameType": "R"},
            timeout=30,
        )
        resp.raise_for_status()
        people = resp.json().get("people", [])
        db: dict[str, int] = {}
        for p in people:
            name = p.get("fullName", "")
            pid = p.get("id")
            if name and pid:
                db[name.lower()] = pid
        _MLB_PLAYER_DB = db
        logger.info("MLB player DB built: %d active players for %d season", len(db), year)
    except Exception as exc:
        logger.warning("MLB player DB build failed: %s", exc)

    try:
        tresp = requests.get(
            f"{MLB_API}/teams",
            params={"sportId": 1},
            timeout=15,
        )
        tresp.raise_for_status()
        teams = tresp.json().get("teams", [])
        _MLB_TEAM_DB = {
            t["id"]: t["abbreviation"]
            for t in teams
            if t.get("id") and t.get("abbreviation")
        }
        logger.info("MLB team DB built: %d teams", len(_MLB_TEAM_DB))
    except Exception as exc:
        logger.warning("MLB team DB build failed: %s", exc)


class MLBStatsClient(BaseStatsClient):

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
        if not _MLB_DB_LOADED:
            _build_mlb_player_db()

        if not _MLB_PLAYER_DB:
            logger.warning("MLB player DB is empty — cannot find '%s'", player_name)
            return None

        name_lower = player_name.strip().lower()

        # 1. Exact full-name match
        if name_lower in _MLB_PLAYER_DB:
            return _MLB_PLAYER_DB[name_lower]

        # 2. First + last name partial match (handles middle names / suffixes)
        parts = name_lower.split()
        if len(parts) >= 2:
            first, last = parts[0], parts[-1]
            candidates = {
                k: v for k, v in _MLB_PLAYER_DB.items()
                if k.endswith(last) and k.startswith(first)
            }
            if len(candidates) == 1:
                return next(iter(candidates.values()))
            if len(candidates) > 1:
                return min(candidates.items(), key=lambda kv: abs(len(kv[0]) - len(name_lower)))[1]

            # 3. Last-name-only fallback
            last_only = {k: v for k, v in _MLB_PLAYER_DB.items() if k.endswith(f" {last}")}
            if len(last_only) == 1:
                return next(iter(last_only.values()))

        logger.warning("MLB: player '%s' not found in player DB", player_name)
        return None

    # -------------------------------------------------------------------------
    # Game log
    # -------------------------------------------------------------------------

    def get_player_game_log(self, player_id: int) -> pd.DataFrame:
        if player_id in self._game_log_cache:
            return self._game_log_cache[player_id]

        # 1. Check disk cache first (valid for 24 hours).
        cached = _GAME_LOG_CACHE.get("MLB", player_id)
        if cached is not None:
            logger.debug("MLB game log for player %d served from disk cache", player_id)
            self._game_log_cache[player_id] = cached
            return cached

        # 2. Try live MLB Stats API.
        year = date.today().year
        # Fetch regular season + postseason so the log is complete during October playoffs.
        hitting_dfs = [self._fetch_group(player_id, year, "hitting", gt) for gt in ("R", "P")]
        time.sleep(0.3)
        pitching_dfs = [self._fetch_group(player_id, year, "pitching", gt) for gt in ("R", "P")]

        hitting_df = pd.concat([d for d in hitting_dfs if not d.empty], ignore_index=True) if any(not d.empty for d in hitting_dfs) else pd.DataFrame()
        pitching_df = pd.concat([d for d in pitching_dfs if not d.empty], ignore_index=True) if any(not d.empty for d in pitching_dfs) else pd.DataFrame()

        if hitting_df.empty and pitching_df.empty:
            df = pd.DataFrame()
        elif hitting_df.empty:
            df = pitching_df
        elif pitching_df.empty:
            df = hitting_df
        else:
            # Merge SO (pitcher strikeouts) into hitting rows where date matches
            merged = hitting_df.merge(
                pitching_df[["GAME_DATE", "SO"]].rename(columns={"SO": "_SO_p"}),
                on="GAME_DATE",
                how="left",
            )
            merged["SO"] = merged["_SO_p"].fillna(merged["SO"]).fillna(0.0)
            merged = merged.drop(columns=["_SO_p"])
            df = merged

        if not df.empty:
            df = df.sort_values("GAME_DATE", ascending=False).reset_index(drop=True)

        # 3. On success, persist to disk cache.
        if not df.empty:
            _GAME_LOG_CACHE.set("MLB", player_id, df)
        else:
            # 4. Live fetch returned nothing — try stale cache (< 7 days old).
            stale = _GAME_LOG_CACHE.get_stale("MLB", player_id)
            if stale is not None:
                df = stale

        self._game_log_cache[player_id] = df
        return df

    def _fetch_group(
        self,
        player_id: int,
        year: int,
        group: str,
        game_type: str = "R",
    ) -> pd.DataFrame:
        try:
            resp = requests.get(
                f"{MLB_API}/people/{player_id}/stats",
                params={
                    "stats": "gameLog",
                    "season": year,
                    "group": group,
                    "gameType": game_type,
                },
                timeout=15,
            )
            if resp.status_code in (404, 204):
                return pd.DataFrame()
            resp.raise_for_status()
            data = resp.json()
            splits = (data.get("stats") or [{}])[0].get("splits", [])
        except Exception as exc:
            logger.debug("MLB game log (%s/%s) failed for player %d: %s", group, game_type, player_id, exc)
            return pd.DataFrame()

        rows = []
        for s in splits:
            stat = s.get("stat", {})
            team_obj = s.get("team", {})
            team_abbr = team_obj.get("abbreviation", "")
            if not team_abbr:
                team_abbr = _MLB_TEAM_DB.get(team_obj.get("id"), "")
            opp_abbr = s.get("opponent", {}).get("abbreviation", "")
            is_home = s.get("isHome", True)
            vs_str = "vs." if is_home else "@"
            row: dict = {
                "GAME_DATE": pd.to_datetime(s.get("date", ""), errors="coerce"),
                "MATCHUP": f"{team_abbr} {vs_str} {opp_abbr}",
                "location": "Home" if is_home else "Away",
                "opponent_abbr": opp_abbr,
                "H":   float(stat.get("hits", 0)),
                "HR":  float(stat.get("homeRuns", 0)),
                "RBI": float(stat.get("rbi", 0)),
                "R":   float(stat.get("runs", 0)),
                "SB":  float(stat.get("stolenBases", 0)),
                "TB":  float(stat.get("totalBases", 0)),
                "SO":  float(stat.get("strikeOuts", 0)),
            }
            rows.append(row)

        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"], errors="coerce")
        return df.dropna(subset=["GAME_DATE"])

    # -------------------------------------------------------------------------
    # Team stats
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
                for col in ("H", "HR", "RBI", "R", "SB", "TB", "SO")
                if col in row.index
            }
            stats["played"] = True
            return stats
        except Exception as exc:
            logger.warning("MLB get_game_stats_for_date failed (player %d): %s", player_id, exc)
            return None
