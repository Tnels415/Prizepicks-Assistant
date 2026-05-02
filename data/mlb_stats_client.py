from __future__ import annotations

import logging
import time
from datetime import date

import pandas as pd
import requests

from data.base_stats_client import BaseStatsClient

logger = logging.getLogger(__name__)

MLB_API = "https://statsapi.mlb.com/api/v1"


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

        player_id = self._search_player(player_name)
        self._player_id_cache[player_name] = player_id
        return player_id

    def _search_player(self, player_name: str) -> int | None:
        try:
            resp = requests.get(
                f"{MLB_API}/people/search",
                params={"names": player_name, "sportId": 1},
                timeout=10,
            )
            resp.raise_for_status()
            people = resp.json().get("people", [])
            if not people:
                logger.warning("MLB: no results for player '%s'", player_name)
                return None
            # Prefer active players
            active = [p for p in people if p.get("active", False)]
            return (active[0] if active else people[0])["id"]
        except Exception as exc:
            logger.warning("MLB player search failed for '%s': %s", player_name, exc)
            return None

    # -------------------------------------------------------------------------
    # Game log
    # -------------------------------------------------------------------------

    def get_player_game_log(self, player_id: int) -> pd.DataFrame:
        if player_id in self._game_log_cache:
            return self._game_log_cache[player_id]

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
            team_abbr = s.get("team", {}).get("abbreviation", "")
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
