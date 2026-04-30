from __future__ import annotations

import logging

import pandas as pd

from config import OPPONENT_STAT_COL, SPORT_CONFIG

logger = logging.getLogger(__name__)

# Default NBA counting stats (used when no sport_config provided)
_NBA_COUNTING_STATS = SPORT_CONFIG["NBA"]["counting_stats"]


class FactorScorer:

    def __init__(self, sport_config: dict | None = None) -> None:
        cfg = sport_config or SPORT_CONFIG["NBA"]
        self._opponent_stat_col: dict = cfg.get("opponent_stat_col", OPPONENT_STAT_COL)
        self._counting_stats: set = cfg.get("counting_stats", _NBA_COUNTING_STATS)

    # -------------------------------------------------------------------------
    # Individual factor adjustments (returns signed pp delta)
    # -------------------------------------------------------------------------

    def score_season_avg_vs_line(self, season_avg: float, line: float) -> float:
        """±10pp based on how far season average is above/below the line."""
        if line <= 0:
            return 0.0
        pct_diff = (season_avg - line) / line
        return float(max(-10.0, min(10.0, pct_diff * 50.0)))

    def score_recent_form(
        self,
        last_5_avg: float,
        last_10_avg: float,
        line: float,
    ) -> float:
        """±8pp weighted blend of last 5 (60%) and last 10 (40%) vs line."""
        if line <= 0:
            return 0.0
        blend = 0.6 * last_5_avg + 0.4 * last_10_avg
        pct_diff = (blend - line) / line
        return float(max(-8.0, min(8.0, pct_diff * 40.0)))

    def score_h2h_this_season(
        self,
        h2h_hit_rate: float,
        h2h_sample_size: int,
        h2h_reliable: bool,
    ) -> float:
        """±8pp based on this-season H2H hit rate vs 50% baseline. Zero if unreliable (<2 games)."""
        if not h2h_reliable or h2h_sample_size < 2:
            return 0.0
        deviation = h2h_hit_rate - 0.5
        return float(max(-8.0, min(8.0, deviation * 16.0)))

    def score_opponent_defense(
        self,
        team_stats_df: pd.DataFrame,
        stat_type: str,
        opponent_team_id: int,
    ) -> tuple[float, int | None]:
        """
        Returns (adjustment pp, rank).
        Rank 1 = best defense (allows fewest) → negative adjustment.
        Rank N = worst defense → positive adjustment.
        Returns (0.0, None) if team stats not available (non-NBA sports).
        """
        if team_stats_df is None or team_stats_df.empty:
            return 0.0, None

        opp_col = self._opponent_stat_col.get(stat_type)
        if not opp_col or opp_col not in team_stats_df.columns:
            if "DEF_RATING" in team_stats_df.columns and "TEAM_ID" in team_stats_df.columns:
                ranked = team_stats_df.sort_values("DEF_RATING", ascending=True).reset_index(drop=True)
                row = ranked[ranked["TEAM_ID"] == opponent_team_id]
                if row.empty:
                    return 0.0, None
                rank = int(row.index[0]) + 1
                n = len(ranked)
                adjustment = ((rank - (n + 1) / 2) / max(n / 2 - 0.5, 1)) * 6.0
                return float(adjustment), rank
            return 0.0, None

        if "TEAM_ID" not in team_stats_df.columns:
            return 0.0, None

        ranked = team_stats_df.sort_values(opp_col, ascending=True).reset_index(drop=True)
        row = ranked[ranked["TEAM_ID"] == opponent_team_id]
        if row.empty:
            return 0.0, None

        rank = int(row.index[0]) + 1
        n = len(ranked)
        adjustment = ((rank - (n + 1) / 2) / max(n / 2 - 0.5, 1)) * 6.0
        return float(adjustment), rank

    def score_home_away(
        self,
        location: str,
        location_avg: float | None,
        line: float,
    ) -> float:
        """±4pp based on how the player performs in this location vs line."""
        if location_avg is None or line <= 0:
            return 0.0
        pct_diff = (location_avg - line) / line
        return float(max(-4.0, min(4.0, pct_diff * 20.0)))

    def score_rest_days(self, rest_days: int) -> float:
        """Back-to-back (0 rest) is tiring; more rest is slightly positive."""
        if rest_days == 0:
            return -3.0
        if rest_days == 1:
            return 0.0
        return 2.0

    def score_pace(
        self,
        team_stats_df: pd.DataFrame,
        player_team_id: int,
        opponent_team_id: int,
        stat_type: str,
        league_avg_pace: float = 99.5,
    ) -> float:
        """±3pp for pace above/below league average. Only applies to counting stats."""
        if stat_type not in self._counting_stats:
            return 0.0
        if team_stats_df is None or team_stats_df.empty or "PACE" not in team_stats_df.columns:
            return 0.0

        def get_pace(tid: int) -> float | None:
            row = team_stats_df[team_stats_df["TEAM_ID"] == tid]
            if row.empty:
                return None
            return float(row["PACE"].iloc[0])

        team_pace = get_pace(player_team_id)
        opp_pace = get_pace(opponent_team_id)
        if team_pace is None or opp_pace is None:
            return 0.0

        combined = (team_pace + opp_pace) / 2.0
        diff = combined - league_avg_pace
        return float(max(-3.0, min(3.0, diff)))

    # -------------------------------------------------------------------------
    # Composite probability
    # -------------------------------------------------------------------------

    def compute_composite_probability(
        self,
        hit_rate_20: float,
        adjustments: list[float],
        learned_weights: dict[str, float] | None = None,
    ) -> float:
        base = hit_rate_20 * 100.0
        if learned_weights:
            from learning.history_store import FACTOR_NAMES
            weighted_sum = sum(
                adj * learned_weights.get(FACTOR_NAMES[i], 1.0)
                for i, adj in enumerate(adjustments)
            )
        else:
            weighted_sum = sum(adjustments)
        total = base + weighted_sum
        return float(max(5.0, min(95.0, total)))
