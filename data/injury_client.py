from __future__ import annotations

"""Player injury designation client.

Fetches current injury/roster status from the ESPN athlete API for all four
supported sports. Returns the official designation string (e.g. "Day-To-Day",
"Questionable", "Out") when a player has any active limitation, or None when
the player is healthy or status cannot be determined.

Results are cached for 1 hour per (player_name, sport) pair. On any fetch
failure the function returns None so downstream suppression is skipped and
the rest of the system is unaffected.
"""

import logging
import time

import requests

logger = logging.getLogger(__name__)

_ESPN_SEARCH_URL = "https://site.api.espn.com/apis/common/v3/search"
_ESPN_ATHLETE_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/athletes/{athlete_id}"
)

# Browser-like headers — ESPN blocks the default Python-requests User-Agent.
_ESPN_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.espn.com/",
    "Origin": "https://www.espn.com",
}

_SPORT_ESPN_MAP: dict[str, tuple[str, str]] = {
    "NBA": ("basketball", "nba"),
    "NHL": ("hockey", "nhl"),
    "MLB": ("baseball", "mlb"),
    "NFL": ("football", "nfl"),
}

# (player_name_lower, sport) → (designation_str | None, timestamp)
_INJURY_CACHE: dict[tuple[str, str], tuple[str | None, float]] = {}
_CACHE_TTL = 3600  # 1 hour

_INJURY_API_UNAVAILABLE: bool = False


def _espn_athlete_id(player_name: str, sport_slug: str, league_slug: str) -> str | None:
    """Return ESPN athlete ID for a player, or None on failure."""
    try:
        resp = requests.get(
            _ESPN_SEARCH_URL,
            params={
                "query": player_name,
                "limit": 5,
                "type": "athlete",
                "sport": sport_slug,
                "league": league_slug,
            },
            headers=_ESPN_HEADERS,
            timeout=8,
        )
        resp.raise_for_status()
        for item in resp.json().get("items", []):
            uid = item.get("uid", "")
            if "~a:" in uid:
                return uid.split("~a:")[-1]
    except Exception as exc:
        logger.info("Injury: ESPN athlete search failed for '%s': %s", player_name, exc)
    return None


def _espn_injury_status(
    athlete_id: str, sport_slug: str, league_slug: str
) -> str | None:
    """Return designation string if player has any injury/limitation, else None."""
    global _INJURY_API_UNAVAILABLE
    try:
        url = _ESPN_ATHLETE_URL.format(
            sport=sport_slug, league=league_slug, athlete_id=athlete_id
        )
        resp = requests.get(url, headers=_ESPN_HEADERS, timeout=8)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        athlete = resp.json().get("athlete", {})

        # Signal 1: injuries array — any entry means the player has a designation.
        injuries = athlete.get("injuries", [])
        if injuries:
            designation = injuries[0].get("status") or injuries[0].get("type") or "Injured"
            return str(designation)

        # Signal 2: injuryStatus / injured — ESPN often uses these top-level fields
        # for in-season designations like "Questionable" even when injuries[] is empty.
        injury_status_str = (
            athlete.get("injuryStatus")
            or athlete.get("injuryStatusName")
        )
        if injury_status_str:
            return str(injury_status_str)

        if athlete.get("injured", False):
            return "Injured"

        # Signal 3: status object — type != "active" means limited/out.
        status = athlete.get("status", {})
        status_type = str(status.get("type", "active")).lower()
        if status_type not in ("active", ""):
            return str(status.get("name", status_type))

    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code and status_code >= 500:
            _INJURY_API_UNAVAILABLE = True
            logger.warning(
                "ESPN injury API returned HTTP %s — disabling for this run", status_code
            )
        else:
            logger.info("Injury: ESPN athlete status fetch failed (id=%s): %s", athlete_id, exc)

    return None


def get_player_injury_status(player_name: str, sport: str) -> str | None:
    """Return the player's current injury designation, or None if healthy/unknown.

    Any non-None return value (e.g. "Day-To-Day", "Questionable", "Out") means
    the player has an active limitation and their props should be suppressed.

    Results are cached for 1 hour. On any API failure returns None so the caller
    treats the player as healthy (safe default).
    """
    global _INJURY_API_UNAVAILABLE
    if _INJURY_API_UNAVAILABLE:
        return None

    slugs = _SPORT_ESPN_MAP.get(sport)
    if slugs is None:
        return None
    sport_slug, league_slug = slugs

    cache_key = (player_name.lower(), sport)
    now = time.time()
    cached = _INJURY_CACHE.get(cache_key)
    if cached is not None and (now - cached[1]) < _CACHE_TTL:
        logger.debug(
            "Injury cache hit for %s (%s): %s", player_name, sport, cached[0]
        )
        return cached[0]

    athlete_id = _espn_athlete_id(player_name, sport_slug, league_slug)
    if athlete_id is None:
        # Don't cache search failures — the search endpoint may be transiently
        # unavailable, and caching None would suppress injury checks for 1 hour
        # even if the endpoint recovers or the designation is added later.
        return None

    designation = _espn_injury_status(athlete_id, sport_slug, league_slug)

    if designation is not None:
        logger.info(
            "Injury status for %s (%s): %s", player_name, sport, designation
        )

    _INJURY_CACHE[cache_key] = (designation, now)
    return designation
