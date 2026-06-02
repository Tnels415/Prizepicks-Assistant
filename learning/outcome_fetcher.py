from __future__ import annotations

import logging
import time
from datetime import date

import pandas as pd

from data.base_stats_client import BaseStatsClient
from data.game_log_archive import GameLogArchive
from learning.history_store import HistoryStore

_ARCHIVE = GameLogArchive()

logger = logging.getLogger(__name__)


class OutcomeFetcher:

    def __init__(
        self,
        store: HistoryStore,
        sport_clients: dict[str, BaseStatsClient] | None = None,
        nba_api_timeout: int = 30,
    ) -> None:
        self._store = store
        self._sport_clients = sport_clients or {}
        self._timeout = nba_api_timeout

    def evaluate_yesterday(self, yesterday: date) -> int:
        """
        Evaluate all unevaluated predictions from yesterday across all sports.
        Returns total count of evaluated predictions.
        """
        unevaluated = self._store.get_unevaluated_predictions(yesterday)
        if not unevaluated:
            logger.info("No unevaluated predictions found for %s", yesterday.isoformat())
            return 0

        # Group by sport
        by_sport: dict[str, list[dict]] = {}
        for row in unevaluated:
            sport = row.get("sport", "NBA")
            by_sport.setdefault(sport, []).append(row)

        total_evaluated = 0
        for sport, rows in by_sport.items():
            client = self._sport_clients.get(sport)
            if client is None:
                logger.debug("No stats client for sport '%s' — skipping evaluation", sport)
                continue
            n = self._evaluate_sport_predictions(rows, client, yesterday, sport)
            total_evaluated += n

        return total_evaluated

    def _evaluate_sport_predictions(
        self,
        unevaluated: list[dict],
        client: BaseStatsClient,
        yesterday: date,
        sport: str,
    ) -> int:
        from config import SPORT_CONFIG
        sport_cfg = SPORT_CONFIG.get(sport, {})
        outcome_stat_map = sport_cfg.get("outcome_stat_map", {})

        # Group by player to minimize API calls
        players: dict[str, int | None] = {}
        for row in unevaluated:
            players[row["player_name"]] = row.get("player_id")

        # Fetch actuals once per player
        actuals_cache: dict[str, dict | None] = {}
        for player_name, player_id in players.items():
            if player_id is None:
                player_id = client.find_player_id(player_name)
                if player_id:
                    self._store.update_player_id(player_name, player_id)

            if player_id is None:
                logger.warning("Cannot evaluate %s — no player ID", player_name)
                actuals_cache[player_name] = None
                continue

            try:
                actuals = client.get_game_stats_for_date(player_id, yesterday)
                actuals_cache[player_name] = actuals
                # Persist settled actuals into the permanent archive.
                if actuals and actuals.get("played", True):
                    row = {k: v for k, v in actuals.items() if k != "played"}
                    row["GAME_DATE"] = pd.Timestamp(yesterday)
                    _ARCHIVE.merge(sport, player_id, pd.DataFrame([row]))
                time.sleep(0.4)
            except Exception as exc:
                logger.warning("Failed to fetch actuals for %s (%s): %s", player_name, sport, exc)
                actuals_cache[player_name] = None

        evaluated = 0
        for row in unevaluated:
            player_name = row["player_name"]
            actuals = actuals_cache.get(player_name)

            if actuals is None:
                continue  # fetch error — retry next run

            actual_value, correct = self._resolve_outcome(row, actuals, outcome_stat_map)

            self._store.record_outcome(row["id"], actual_value, correct)
            evaluated += 1

            status = {1: "CORRECT", 0: "WRONG", -1: "DNP"}.get(correct, "?")
            logger.debug(
                "  [%s] %s %s %s (line %.1f) → actual %.1f  [%s]",
                sport, player_name, row["direction"], row["stat_type"],
                row["line"], actual_value, status,
            )

        logger.info(
            "Evaluated %d/%d %s predictions for %s",
            evaluated, len(unevaluated), sport, yesterday.isoformat(),
        )
        return evaluated

    @staticmethod
    def _resolve_outcome(
        prediction: dict,
        actuals: dict | None,
        outcome_stat_map: dict,
    ) -> tuple[float, int]:
        if actuals is None:
            return 0.0, -1

        if not actuals.get("played", True):
            return -1.0, -1

        stat_type = prediction["stat_type"]
        cols = outcome_stat_map.get(stat_type)

        if cols is None:
            # Try prop_stat_map fallback: use stat_type key directly
            col_guess = stat_type[:3].upper()
            actual_value = actuals.get(col_guess, 0.0)
        elif isinstance(cols, list):
            actual_value = sum(actuals.get(c, 0.0) for c in cols)
        else:
            actual_value = actuals.get(cols, 0.0)

        line = prediction["line"]
        direction = prediction["direction"]

        if direction == "OVER":
            correct = 1 if actual_value > line else 0
        else:
            correct = 1 if actual_value < line else 0

        return float(actual_value), correct
