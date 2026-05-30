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
        recency_half_life_days: int = 365,
    ) -> dict:
        """Filter the full (multi-season) game log for games vs. opponent_abbr.

        Applies exponential recency weighting so older seasons contribute less.
        A half_life_days of 365 means games from a year ago count half as much.
        Returns hit_rate, avg, and sample_size. Zeroes out if sample_size < 2.
        """
        if "opponent_abbr" not in df.columns or not opponent_abbr:
            return {"hit_rate": 0.5, "avg": 0.0, "sample_size": 0, "reliable": False}

        mask = df["opponent_abbr"].fillna("").astype(str).str.upper() == opponent_abbr.upper()
        h2h_df = df[mask].copy()

        if h2h_df.empty:
            return {"hit_rate": 0.5, "avg": 0.0, "sample_size": 0, "reliable": False}

        series = self.get_stat_series(h2h_df, stat_type).dropna()
        if series.empty:
            return {"hit_rate": 0.5, "avg": 0.0, "sample_size": 0, "reliable": False}

        # Build recency weights from GAME_DATE if available.
        if "GAME_DATE" in h2h_df.columns:
            import pandas as pd
            from datetime import date
            today = pd.Timestamp(date.today())
            aligned = h2h_df.loc[series.index, "GAME_DATE"] if "GAME_DATE" in h2h_df.columns else None
            if aligned is not None:
                ages_days = (today - pd.to_datetime(aligned)).dt.days.clip(lower=0)
                weights = 0.5 ** (ages_days / recency_half_life_days)
                weights = weights.fillna(1.0).values
            else:
                weights = None
        else:
            weights = None

        if weights is not None and len(weights) == len(series):
            import numpy as np
            w = weights / weights.sum()
            hit_rate = float(np.dot((series.values > line).astype(float), w))
            avg = float(np.dot(series.values, w))
        else:
            hit_rate = float((series > line).sum()) / len(series)
            avg = float(series.mean())

        reliable = len(series) >= 2
        return {
            "hit_rate": hit_rate,
            "avg": avg,
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

    def calculate_consistency(self, df: pd.DataFrame, stat_type: str, line: float) -> dict:
        """Return volatility metrics: std_dev, coefficient of variation, and
        the fraction of games that fell within one std-dev of the line."""
        series = self.get_stat_series(df, stat_type).dropna()
        if len(series) < 5:
            return {"std_dev": 0.0, "cv": 0.0, "pct_within_1std_of_line": 0.5}
        std = float(series.std())
        mean = float(series.mean())
        cv = std / mean if mean > 0 else 0.0
        within_1std = float(((series - line).abs() <= std).mean())
        return {"std_dev": std, "cv": cv, "pct_within_1std_of_line": within_1std}

    def calculate_hit_rates_multi_window(
        self, df: pd.DataFrame, stat_type: str, line: float
    ) -> dict:
        """Return prop-specific hit rates at 3 recency windows against the exact line."""
        series = self.get_stat_series(df, stat_type).dropna()

        def _hr(n: int) -> float:
            s = series.iloc[:n]
            return float((s > line).mean()) if len(s) >= 3 else 0.5

        return {"hr_5": _hr(5), "hr_10": _hr(10), "hr_20": _hr(20)}
