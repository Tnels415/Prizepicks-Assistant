"""
Multi-book consensus market odds via The Odds API (player props).

The single strongest predictor of a prop outcome is the sharp, devigged market
probability.  A *consensus* across several books is more robust than any one
book (it averages out each book's idiosyncratic lean and soft spots), so this
client pulls player-prop odds from every US book The Odds API exposes and
returns a consensus de-vigged probability per (player, stat_type).

The Odds API player-prop markets are per-event: you first list today's events
for a sport, then request odds for each event with the desired player-prop
market keys.  Each event request costs `len(markets) * len(regions)` credits,
so this client is quota-aware — it caps the number of events per run and fails
gracefully (empty map) when the key is missing, exhausted, or the network is
down.  An empty map simply means the model runs unanchored.
"""
from __future__ import annotations

import logging
import os
from datetime import date

import requests

from analysis import market as _market
from data.draftkings_client import _normalize_name

logger = logging.getLogger(__name__)

_ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Cap events per run so a busy slate can't blow the monthly quota in one shot.
# Each event costs len(markets) credits (one region: us).  At ~7 NBA markets
# that's 7 credits/event → 20 events = 140 credits per sport per run.
_MAX_EVENTS_PER_RUN = 20


class OddsAPIMarketClient:
    """Fetches consensus player-prop odds across US books from The Odds API."""

    def __init__(self, api_key: str | None = None, timeout: int = 12) -> None:
        self._api_key = api_key or os.getenv("THE_ODDS_API_KEY", "")
        self._timeout = timeout

    def fetch_consensus_odds(
        self, sport_config: dict
    ) -> dict[tuple[str, str], dict]:
        """Return {(normalized_player, stat_type): {...}} consensus odds.

        Each value carries:
            line        — the modal (most common) line across books
            p_over      — consensus de-vigged P(over) in percent [0, 100]
            p_under     — 100 - p_over
            n_books     — number of books contributing to the consensus
        Returns an empty dict on any failure (missing key, quota, network).
        """
        sport_name = sport_config["name"]
        sport_key = sport_config["odds_sport_key"]
        markets = sport_config.get("markets") or []
        market_to_stat = sport_config.get("market_to_stat") or {}

        if not self._api_key:
            logger.debug("Odds API key not set — skipping consensus market odds for %s", sport_name)
            return {}
        if not markets:
            logger.debug("No Odds API markets configured for %s — skipping", sport_name)
            return {}

        events = self._fetch_today_events(sport_key, sport_name)
        if not events:
            return {}

        # quote_groups[(player, stat, line)] = list of per-book p_over (devigged)
        quote_groups: dict[tuple[str, str, float], list[float]] = {}

        for event_id in events[:_MAX_EVENTS_PER_RUN]:
            event_odds = self._fetch_event_odds(sport_key, event_id, markets, sport_name)
            if not event_odds:
                continue
            self._collect_quotes(event_odds, market_to_stat, quote_groups)

        odds_map = self._build_consensus(quote_groups)
        logger.info(
            "Odds API: %d %s consensus market lines available for blending "
            "(across %d event(s))",
            len(odds_map), sport_name, min(len(events), _MAX_EVENTS_PER_RUN),
        )
        return odds_map

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _fetch_today_events(self, sport_key: str, sport_name: str) -> list[str]:
        try:
            resp = requests.get(
                f"{_ODDS_API_BASE}/sports/{sport_key}/events",
                params={"apiKey": self._api_key, "dateFormat": "iso"},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            events = resp.json()
        except Exception as exc:
            logger.warning("Odds API events fetch failed for %s: %s", sport_name, exc)
            return []

        today_str = date.today().isoformat()
        ids: list[str] = []
        for ev in events:
            ct = ev.get("commence_time", "")
            if ct and ct[:10] == today_str and ev.get("id"):
                ids.append(str(ev["id"]))
        return ids

    def _fetch_event_odds(
        self, sport_key: str, event_id: str, markets: list[str], sport_name: str
    ) -> dict | None:
        try:
            resp = requests.get(
                f"{_ODDS_API_BASE}/sports/{sport_key}/events/{event_id}/odds",
                params={
                    "apiKey": self._api_key,
                    "regions": "us",
                    "markets": ",".join(markets),
                    "oddsFormat": "american",
                    "dateFormat": "iso",
                },
                timeout=self._timeout,
            )
            if resp.status_code == 422:
                # No player-prop markets offered for this event (common pre-lineup).
                logger.debug("Odds API: no prop markets for %s event %s", sport_name, event_id)
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.debug("Odds API event-odds fetch failed (%s %s): %s", sport_name, event_id, exc)
            return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_quotes(
        event_odds: dict,
        market_to_stat: dict[str, str],
        quote_groups: dict[tuple[str, str, float], list[float]],
    ) -> None:
        """Devig each book's Over/Under for every player line and bucket by line."""
        for book in event_odds.get("bookmakers", []):
            for market in book.get("markets", []):
                stat_type = market_to_stat.get(market.get("key", ""))
                if not stat_type:
                    continue
                # Pair Over/Under outcomes by player (description) + point (line).
                by_player: dict[tuple[str, float], dict[str, float]] = {}
                for oc in market.get("outcomes", []):
                    name = str(oc.get("name", "")).strip().lower()   # "over" / "under"
                    player = str(oc.get("description", "")).strip()
                    point = oc.get("point")
                    price = oc.get("price")
                    if name not in ("over", "under") or not player or point is None:
                        continue
                    try:
                        line = float(point)
                    except (TypeError, ValueError):
                        continue
                    by_player.setdefault((player, line), {})[name] = price

                for (player, line), sides in by_player.items():
                    if "over" not in sides or "under" not in sides:
                        continue
                    devig = _market.devig_two_way(sides["over"], sides["under"])
                    if devig is None:
                        continue
                    key = (_normalize_name(player), stat_type, line)
                    quote_groups.setdefault(key, []).append(devig[0])

    @staticmethod
    def _build_consensus(
        quote_groups: dict[tuple[str, str, float], list[float]],
    ) -> dict[tuple[str, str], dict]:
        """Collapse per-(player, stat, line) quotes into one consensus per (player, stat).

        When books disagree on the line, the line backed by the most books wins;
        ties break toward the line whose consensus probability is closest to 50%
        (the most liquid / standard line).
        """
        # Group candidate lines by (player, stat).
        by_prop: dict[tuple[str, str], list[tuple[float, list[float]]]] = {}
        for (player, stat, line), p_overs in quote_groups.items():
            by_prop.setdefault((player, stat), []).append((line, p_overs))

        out: dict[tuple[str, str], dict] = {}
        for (player, stat), line_options in by_prop.items():
            # Pick the line with the most book quotes (modal line).
            best_line, best_quotes = max(
                line_options,
                key=lambda lo: (len(lo[1]), -abs((sum(lo[1]) / len(lo[1])) - 50.0)),
            )
            p_over = sum(best_quotes) / len(best_quotes)
            out[(player, stat)] = {
                "line": best_line,
                "p_over": p_over,
                "p_under": 100.0 - p_over,
                "n_books": len(best_quotes),
            }
        return out
