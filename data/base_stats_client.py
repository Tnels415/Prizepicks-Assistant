from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

import pandas as pd


class BaseStatsClient(ABC):
    """
    Protocol that every sport's stats client must satisfy.

    get_player_game_log returns a DataFrame whose columns include at minimum:
        GAME_DATE      - datetime64 column, sorted descending
        MATCHUP        - str: "{TEAM} vs. {OPP}" (home) or "{TEAM} @ {OPP}" (away)
        location       - "Home" or "Away"
        opponent_abbr  - str abbreviation of the opponent team

    Plus sport-specific stat columns (PTS/REB/AST for NBA, G/A/PTS for NHL, etc.)
    """

    @abstractmethod
    def find_player_id(self, player_name: str) -> int | None:
        """Return the sport's internal integer player ID, or None if not found."""

    @abstractmethod
    def get_player_game_log(self, player_id: int) -> pd.DataFrame:
        """Return current-season game log DataFrame (sorted newest first)."""

    @abstractmethod
    def get_team_advanced_stats(self) -> pd.DataFrame:
        """
        Return a DataFrame of team-level stats used for opponent defense and
        pace scoring. Return empty DataFrame if not available for this sport.
        """

    def get_player_usage(self) -> dict[int, float]:
        """Return {player_id: usage_pct} dict. Default: empty (not all sports have this)."""
        return {}

    def get_player_splits(self, player_id: int) -> dict:
        """Return home/away and rest-day splits. Default: empty."""
        return {}

    def get_game_stats_for_date(
        self,
        player_id: int,
        game_date: date,
    ) -> dict | None:
        """
        Return actual stat dict for player on game_date (for outcome evaluation).
        Returns None on fetch error, {"played": False} if player had no game.
        Keys should match the sport's prop_stat_map values.
        """
        try:
            df = self.get_player_game_log(player_id)
            if df.empty or "GAME_DATE" not in df.columns:
                return {"played": False}
            mask = df["GAME_DATE"].dt.date == game_date
            rows = df[mask]
            if rows.empty:
                return {"played": False}
            row = rows.iloc[0]
            stat_cols = [c for c in df.columns
                         if c not in ("GAME_DATE", "MATCHUP", "location", "opponent_abbr")]
            stats = {c: float(row[c]) for c in stat_cols if c in row.index}
            stats["played"] = True
            return stats
        except Exception:
            return None
