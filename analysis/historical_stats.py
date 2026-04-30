from __future__ import annotations

import logging
from datetime import timedelta

import numpy as np
import pandas as pd

from config import PROP_STAT_MAP, COMBO_STAT_MAP

logger = logging.getLogger(__name__)


class HistoricalStatsCalculator:

    def __init__(
        self,
        prop_stat_map: dict | None = None,
        combo_stat_map: dict | None = None,
    ) -> None:
        self._prop_stat_map = prop_stat_map if prop_stat_map is not None else PROP_STAT_MAP
        self._combo_stat_map = combo_stat_map if combo_stat_map is not None else COMBO_STAT_MAP

    def get_stat_series(self, df: pd.DataFrame, stat_type: str) -> pd.Series:
        if stat_type in self._combo_stat_map:
            cols = self._combo_stat_map[stat_type]
            available = [c for c in cols if c in df.columns]
            if not available:
                return pd.Series(dtype=float)
            series = df[available[0]].copy().astype(float)
            for col in available[1:]:
                series = series + df[col].astype(float)
            return series

        col = self._prop_stat_map.get(stat_type)
        if col and col in df.columns:
            return df[col].astype(float)

        logger.debug("No column mapping for stat_type: %s", stat_type)
        return pd.Series(dtype=float)

    def calculate_hit_rate(
        self,
        df: pd.DataFrame,
        stat_type: str,
        line: float,
        last_n: int = 20,
    ) -> tuple[float, int]:
        """Returns (hit_rate 0.0–1.0, games_counted)."""
        series = self.get_stat_series(df, stat_type)
        if series.empty:
            return 0.5, 0
        series = series.dropna().head(last_n)
        if len(series) == 0:
            return 0.5, 0
        hit_rate = float((series > line).sum()) / len(series)
        return hit_rate, len(series)

    def calculate_averages(self, df: pd.DataFrame, stat_type: str) -> dict:
        series = self.get_stat_series(df, stat_type).dropna()
        if series.empty:
            return {"season_avg": 0.0, "last_5_avg": 0.0, "last_10_avg": 0.0,
                    "last_20_avg": 0.0, "games_played": 0}
        return {
            "season_avg": float(series.mean()),
            "last_5_avg": float(series.head(5).mean()) if len(series) >= 1 else 0.0,
            "last_10_avg": float(series.head(10).mean()) if len(series) >= 1 else 0.0,
            "last_20_avg": float(series.head(20).mean()) if len(series) >= 1 else 0.0,
            "games_played": len(series),
        }

    def calculate_h2h_stats(
        self,
        df: pd.DataFrame,
        stat_type: str,
        line: float,
        opponent_abbr: str,
    ) -> dict:
        """
        Filters the current-season game log for games vs. opponent_abbr this season.
        Returns hit_rate, avg, and sample_size. Zeroes out if sample_size < 2.
        """
        if "opponent_abbr" not in df.columns or not opponent_abbr:
            return {"hit_rate": 0.5, "avg": 0.0, "sample_size": 0, "reliable": False}

        mask = df["opponent_abbr"].str.upper() == opponent_abbr.upper()
        h2h_df = df[mask].copy()

        if h2h_df.empty:
            return {"hit_rate": 0.5, "avg": 0.0, "sample_size": 0, "reliable": False}

        series = self.get_stat_series(h2h_df, stat_type).dropna()
        if series.empty:
            return {"hit_rate": 0.5, "avg": 0.0, "sample_size": 0, "reliable": False}

        hit_rate = float((series > line).sum()) / len(series)
        reliable = len(series) >= 2
        return {
            "hit_rate": hit_rate,
            "avg": float(series.mean()),
            "sample_size": len(series),
            "reliable": reliable,
        }

    def infer_rest_days(self, df: pd.DataFrame) -> int:
        """Return rest days before the most recent game based on GAME_DATE column."""
        if "GAME_DATE" not in df.columns or len(df) < 2:
            return 2
        dates = df["GAME_DATE"].dropna().sort_values(ascending=False)
        if len(dates) < 2:
            return 2
        most_recent = dates.iloc[0]
        second = dates.iloc[1]
        delta = (most_recent - second).days - 1
        return max(0, delta)

    def calculate_location_avg(
        self,
        df: pd.DataFrame,
        stat_type: str,
        location: str,
    ) -> float | None:
        if "location" not in df.columns:
            return None
        loc_df = df[df["location"] == location]
        if loc_df.empty:
            return None
        series = self.get_stat_series(loc_df, stat_type).dropna()
        return float(series.mean()) if not series.empty else None
