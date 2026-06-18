from __future__ import annotations

"""
Capture closing market lines for today's picks and compute CLV.

Closing-line value (did our bet beat the line just before tip-off?) is the
single best fast indicator of whether the model is genuinely +EV — you get
signal in hours instead of waiting weeks for win/loss to stabilize.

Run this close to the first game of the slate (a second, late cron):
    0 18 * * *  /usr/bin/python3 /path/to/update_closing_lines.py

It fetches the current multi-book consensus (the closing line proxy), matches it
to each of today's recorded predictions, and stores closing_prob_over + CLV.
"""

import logging
import sys
from datetime import date

from config import get_active_sports, SPORT_CONFIG
from data.odds_api_market import OddsAPIMarketClient
from data.draftkings_client import _normalize_name
from learning.history_store import HistoryStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("update_closing_lines")


def main() -> int:
    today = date.today()
    store = HistoryStore()
    pending = store.get_predictions_needing_closing_line(today)
    if not pending:
        logger.info("No predictions awaiting a closing line for %s", today.isoformat())
        return 0

    # Build a consensus map per active sport (one fetch each).
    client = OddsAPIMarketClient()
    consensus_by_sport: dict[str, dict] = {}
    for sport in get_active_sports():
        try:
            consensus_by_sport[sport] = client.fetch_consensus_odds(SPORT_CONFIG[sport])
        except Exception as exc:
            logger.warning("Closing-line fetch failed for %s: %s", sport, exc)
            consensus_by_sport[sport] = {}

    updated = 0
    for pred in pending:
        sport = pred.get("sport", "NBA")
        cmap = consensus_by_sport.get(sport, {})
        key = (_normalize_name(pred["player_name"]), pred["stat_type"])
        q = cmap.get(key)
        if not q:
            continue
        store.record_closing_line(
            prediction_id=pred["id"],
            direction=pred["direction"],
            hit_probability=pred["hit_probability"],
            closing_line=q["line"],
            closing_prob_over=q["p_over"],
        )
        updated += 1

    logger.info(
        "Captured closing lines for %d/%d pending prediction(s)",
        updated, len(pending),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
