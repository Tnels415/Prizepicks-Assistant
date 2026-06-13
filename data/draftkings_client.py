from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

# DraftKings sportsbook event-group IDs — one per league.
# These map to the public sportsbook page for each sport and are stable
# across seasons (confirmed via community documentation).
DK_EVENT_GROUP_IDS: dict[str, int] = {
    "NBA": 42648,
    "NHL": 42133,
    "NFL": 88808,
    "MLB": 40625,
}

# DraftKings offer-label → our internal stat_type, per sport.
# Only non-obvious mappings are listed; labels that already match our
# stat_type strings verbatim are handled by the fallback in _parse().
DK_STAT_MAP: dict[str, dict[str, str]] = {
    "NBA": {
        # 3-pointers
        "Threes": "3-PT Made",
        "3-Point Made": "3-PT Made",
        "Three Point Made": "3-PT Made",
        "3 Point Made": "3-PT Made",
        # combo markets
        "Pts + Reb + Ast": "Pts+Reb+Ast",
        "Pts+Reb+Ast": "Pts+Reb+Ast",
        "Points + Rebounds + Assists": "Pts+Reb+Ast",
        "Pts + Ast": "Pts+Ast",
        "Pts+Ast": "Pts+Ast",
        "Points + Assists": "Pts+Ast",
        "Pts + Reb": "Pts+Reb",
        "Pts+Reb": "Pts+Reb",
        "Points + Rebounds": "Pts+Reb",
        "Reb + Ast": "Reb+Ast",
        "Reb+Ast": "Reb+Ast",
        "Rebounds + Assists": "Reb+Ast",
    },
    "NHL": {
        # DraftKings capitalises both words; our key uses title-case
        "Shots On Goal": "Shots on Goal",
        "Shots on Goal": "Shots on Goal",
    },
    "MLB": {
        "Pitcher Strikeouts": "Strikeouts",
        "Runs Batted In": "RBIs",
        "Runs": "Runs Scored",
    },
    "NFL": {
        "Passing Touchdowns": "Passing TDs",
        "Pass TDs": "Passing TDs",
        "Pass Yards": "Passing Yards",
        "Rush Yards": "Rushing Yards",
        "Rec Yards": "Receiving Yards",
    },
}

# Multiple header variants — try each in turn if DraftKings Cloudflare
# blocks the first attempt (same pattern as PrizePicksLiveClient).
_DK_HEADER_VARIANTS = [
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://sportsbook.draftkings.com/",
        "Origin": "https://sportsbook.draftkings.com",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.4.1 Safari/605.1.15"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": "https://sportsbook.draftkings.com/",
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.6367.82 Mobile Safari/537.36"
        ),
        "Accept": "application/json",
        "Referer": "https://sportsbook.draftkings.com/",
    },
]

_DK_BASE = "https://sportsbook.draftkings.com/sites/US-SB/api/v5"


def _normalize_name(name: str) -> str:
    """Normalize a player name for cross-source matching (DK ↔ PrizePicks).

    Lowercases, strips punctuation and common suffixes so "Luka Dončić",
    "Luka Doncic" and "P.J. Washington Jr." line up across providers.
    """
    import unicodedata

    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    n = n.lower()
    # Delete intra-word punctuation ("p.j." → "pj"); split the rest on spaces.
    for ch in (".", "'"):
        n = n.replace(ch, "")
    for ch in (",", "-"):
        n = n.replace(ch, " ")
    suffixes = {"jr", "sr", "ii", "iii", "iv", "v"}
    tokens = [t for t in n.split() if t not in suffixes]
    return " ".join(tokens)


class DraftKingsPropsClient:
    """
    Fetches player prop lines from DraftKings' public sportsbook API.
    No API key or authentication required.

    DraftKings groups all markets for a sport under a single event-group
    endpoint.  We iterate every offer category and subcategory in the
    response, match offer labels to our internal stat-type vocabulary, and
    return one prop dict per (player, stat_type) pair using the first
    standard (non-alternate) line we encounter.
    """

    def fetch_props(self, sport_config: dict) -> list[dict]:
        sport_name = sport_config["name"]
        event_group_id = DK_EVENT_GROUP_IDS.get(sport_name)
        if not event_group_id:
            logger.debug("DraftKings: no event-group ID configured for %s", sport_name)
            return []

        prop_stat_map = sport_config["prop_stat_map"]
        dk_stat_map = DK_STAT_MAP.get(sport_name, {})

        data = self._fetch(event_group_id, sport_name)
        if data is None:
            return []

        props = self._parse(data, sport_name, dk_stat_map, prop_stat_map)
        logger.info("DraftKings: %d %s props parsed", len(props), sport_name)
        return props

    def fetch_market_odds(self, sport_config: dict) -> dict[tuple[str, str], dict]:
        """Return a {(normalized_player_name, stat_type): {...}} odds map.

        Each value carries the DraftKings line plus the Over/Under American odds
        so callers can de-vig and blend a market probability into the model.
        Returns an empty dict when DraftKings is unavailable (graceful no-op —
        the model simply runs unanchored).
        """
        props = self.fetch_props(sport_config)
        odds_map: dict[tuple[str, str], dict] = {}
        for p in props:
            if p.get("over_odds") is None or p.get("under_odds") is None:
                continue
            key = (_normalize_name(p["player_name"]), p["stat_type"])
            odds_map[key] = {
                "line": p["line"],
                "over_odds": p["over_odds"],
                "under_odds": p["under_odds"],
            }
        logger.info(
            "DraftKings: %d %s market odds available for blending",
            len(odds_map), sport_config["name"],
        )
        return odds_map

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def _fetch(self, event_group_id: int, sport_name: str) -> dict | None:
        url = f"{_DK_BASE}/eventgroups/{event_group_id}"
        for i, headers in enumerate(_DK_HEADER_VARIANTS):
            try:
                resp = requests.get(
                    url,
                    headers=headers,
                    params={"format": "json"},
                    timeout=8,   # kept short — DK is a non-critical market-odds anchor
                )
                if resp.status_code == 403:
                    logger.debug(
                        "DraftKings 403 for %s (header variant %d) — trying next",
                        sport_name, i + 1,
                    )
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.HTTPError:
                continue
            except Exception as exc:
                logger.warning("DraftKings fetch error for %s (variant %d): %s", sport_name, i + 1, exc)
                break

        logger.warning(
            "DraftKings unavailable for %s — all %d header variants failed (403 or error). "
            "The sportsbook API may have added bot protection.",
            sport_name, len(_DK_HEADER_VARIANTS),
        )
        return None

    # ------------------------------------------------------------------
    # Parse
    # ------------------------------------------------------------------

    def _parse(
        self,
        data: dict,
        sport_name: str,
        dk_stat_map: dict[str, str],
        prop_stat_map: dict[str, str | None],
    ) -> list[dict]:
        # seen[(player_name, stat_type)] = prop_dict
        # Keeps the first standard line; skips alternates.
        seen: dict[tuple[str, str], dict] = {}

        event_group = data.get("eventGroup", {})

        for category in event_group.get("offerCategories", []):
            for subcat_desc in category.get("offerSubcategoryDescriptors", []):
                subcat = subcat_desc.get("offerSubcategory")
                if not subcat:
                    continue

                offers_raw = subcat.get("offers", [])

                # DraftKings returns offers as either:
                #   list[list[dict]]  — each inner list is one player's lines
                #   list[dict]        — flat list of individual offers
                # Normalise to a flat iterator of offer dicts.
                for raw in offers_raw:
                    offer_list = raw if isinstance(raw, list) else [raw]
                    for offer in offer_list:
                        if not isinstance(offer, dict):
                            continue
                        self._process_offer(offer, sport_name, dk_stat_map, prop_stat_map, seen)

        return list(seen.values())

    def _process_offer(
        self,
        offer: dict,
        sport_name: str,
        dk_stat_map: dict[str, str],
        prop_stat_map: dict[str, str | None],
        seen: dict,
    ) -> None:
        label = offer.get("label", "").strip()

        # Map DK label → our stat_type; fall back to the raw label.
        stat_type = dk_stat_map.get(label, label)

        # Skip if this stat isn't in our supported set for this sport.
        if stat_type not in prop_stat_map:
            return
        # Skip stats that map to None (unsupported combos).
        if prop_stat_map[stat_type] is None:
            return

        outcomes = offer.get("outcomes", [])

        # Grab the Over outcome — it carries both player name and line.
        over = next((o for o in outcomes if o.get("name") == "Over"), None)
        if over is None:
            return
        under = next((o for o in outcomes if o.get("name") == "Under"), None)

        player_name = str(over.get("description") or "").strip()
        if not player_name:
            return

        point = over.get("point")
        if point is None:
            return

        try:
            line = float(point)
        except (TypeError, ValueError):
            return

        # American odds for each side, used to de-vig a market probability.
        # Missing/garbled odds leave the field None so the blend is skipped.
        def _odds(outcome: dict | None) -> float | None:
            if not outcome:
                return None
            raw = outcome.get("oddsAmerican")
            if raw is None:
                return None
            try:
                return float(str(raw).replace("+", ""))
            except (TypeError, ValueError):
                return None

        key = (player_name, stat_type)
        if key not in seen:
            seen[key] = {
                "sport": sport_name,
                "projection_id": str(offer.get("providerOfferId", len(seen))),
                "player_name": player_name,
                "team_abbr": "",
                "event_home_abbr": "",
                "event_away_abbr": "",
                "position": "",
                "stat_type": stat_type,
                "line": line,
                "start_time": "",
                "pick_type": "standard",
                "over_odds": _odds(over),
                "under_odds": _odds(under),
            }
