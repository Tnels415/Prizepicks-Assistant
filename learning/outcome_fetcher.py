from __future__ import annotations

import logging
import time
from datetime import date

from learning.history_store import HistoryStore

logger = logging.getLogger(__name__)

# Maps stat_type → game log column(s) for summing
STAT_TO_COLS: dict[str, str | list[str]] = {
    "Points":      "PTS",
    "Rebounds":    "REB",
    "Assists":     "AST",
    "3-PT Made":   "FG3M",
    "Steals":      "STL",
    "Blocks":      "BLK",
    "Turnovers":   "TOV",
    "Pts+Reb+Ast": ["PTS", "REB", "AST"],
    "Pts+Ast":     ["PTS", "AST"],
    "Pts+Reb":     ["PTS", "REB"],
    "Reb+Ast":     ["REB", "AST"],
}


class OutcomeFetcher:

    def __init__(self, store: HistoryStore, nba_api_timeout: int = 30) -> None:
        self._store = store
        self._timeout = nba_api_timeout

    def evaluate_yesterday(self, yesterday: date) -> int:
        unevaluated = self._store.get_unevaluated_predictions(yesterday)
        if not unevaluated:
            logger.info("No unevaluated predictions found for %s", yesterday.isoformat())
            return 0

        # Group by player to minimize API calls
        players: dict[str, int | None] = {}
        for row in unevaluated:
            players[row["player_name"]] = row.get("player_id")

        # Fetch actuals once per player
        actuals_cache: dict[str, dict | None] = {}
        for player_name, player_id in players.items():
            try:
                actuals_cache[player_name] = self._fetch_player_actuals(
                    player_id, player_name, yesterday
                )
                time.sleep(0.6)  # gentle rate-limiting for nba_api
            except Exception as exc:
                logger.warning("Failed to fetch actuals for %s: %s", player_name, exc)
                actuals_cache[player_name] = None

        evaluated = 0
        for row in unevaluated:
            player_name = row["player_name"]
            actuals = actuals_cache.get(player_name)

            actual_value, correct = self._resolve_outcome(row, actuals)

            # actual_value=0, correct=-1 means fetch error → skip, retry next run
            if actuals is None:
                continue

            self._store.record_outcome(row["id"], actual_value, correct)
            evaluated += 1

            status = {1: "CORRECT", 0: "WRONG", -1: "DNP"}[correct]
            logger.debug(
                "  %s %s %s (line %.1f) → actual %.1f  [%s]",
                player_name, row["direction"], row["stat_type"],
                row["line"], actual_value, status,
            )

        logger.info(
            "Evaluated %d/%d predictions for %s",
            evaluated, len(unevaluated), yesterday.isoformat(),
        )
        return evaluated

    def _fetch_player_actuals(
        self,
        player_id: int | None,
        player_name: str,
        game_date: date,
    ) -> dict | None:
        # Resolve player_id if missing
        if player_id is None:
            player_id = self._resolve_player_id(player_name)
            if player_id:
                self._store.update_player_id(player_name, player_id)

        if player_id is None:
            logger.warning("Cannot evaluate %s — no player ID", player_name)
            return None

        date_str = game_date.strftime("%m/%d/%Y")
        endpoint = self._nba_api_call(
            "playergamelog",
            player_id=player_id,
            date_from_nullable=date_str,
            date_to_nullable=date_str,
            timeout=self._timeout,
        )
        if endpoint is None:
            return None

        try:
            df = endpoint.get_data_frames()[0]
        except Exception as exc:
            logger.warning("Could not parse game log for %s: %s", player_name, exc)
            return None

        if df.empty:
            return {"played": False}

        row = df.iloc[0]
        stats = {col: float(row[col]) for col in ["PTS", "REB", "AST", "FG3M", "STL", "BLK", "TOV"]
                 if col in df.columns}
        stats["played"] = True
        return stats

    @staticmethod
    def _resolve_player_id(player_name: str) -> int | None:
        try:
            from nba_api.stats.static import players as nba_players
            matches = nba_players.find_players_by_full_name(player_name)
            if not matches:
                return None
            active = [p for p in matches if p.get("is_active")]
            return (active[0] if active else matches[0])["id"]
        except Exception:
            return None

    def _resolve_outcome(
        self,
        prediction: dict,
        actuals: dict | None,
    ) -> tuple[float, int]:
        if actuals is None:
            return 0.0, -1  # fetch error — caller skips recording

        if not actuals.get("played", True):
            return -1.0, -1  # DNP / postponed

        stat_type = prediction["stat_type"]
        cols = STAT_TO_COLS.get(stat_type, "PTS")

        if isinstance(cols, list):
            actual_value = sum(actuals.get(c, 0.0) for c in cols)
        else:
            actual_value = actuals.get(cols, 0.0)

        line = prediction["line"]
        direction = prediction["direction"]

        if direction == "OVER":
            correct = 1 if actual_value > line else 0
        else:
            correct = 1 if actual_value < line else 0

        return actual_value, correct

    def _nba_api_call(self, endpoint_name: str, **kwargs):
        last_exc = None
        for attempt in range(3):
            try:
                if endpoint_name == "playergamelog":
                    from nba_api.stats.endpoints import playergamelog
                    from config import NBA_SEASON, NBA_SEASON_TYPE
                    return playergamelog.PlayerGameLog(
                        season=NBA_SEASON,
                        season_type_all_star=NBA_SEASON_TYPE,
                        **kwargs,
                    )
            except Exception as exc:
                last_exc = exc
                wait = 2 ** attempt
                logger.debug("nba_api attempt %d failed: %s — retrying in %ds", attempt + 1, exc, wait)
                time.sleep(wait)
        logger.warning("nba_api call failed after 3 attempts: %s", last_exc)
        return None
