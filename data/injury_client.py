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
            timeout=8,
        )
        resp.raise_for_status()
        for item in resp.json().get("items", []):
            uid = item.get("uid", "")
            if "~a:" in uid:
                return uid.split("~a:")[-1]
    except Exception as exc:
        logger.debug("Injury: ESPN athlete search failed for '%s': %s", player_name, exc)
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
        resp = requests.get(url, timeout=8)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        athlete = resp.json().get("athlete", {})

        # Primary signal: injuries array — any entry means the player has a designation.
        injuries = athlete.get("injuries", [])
        if injuries:
            designation = injuries[0].get("status") or injuries[0].get("type") or "Injured"
            return str(designation)

        # Secondary signal: status object — type != "active" means limited/out.
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
            logger.debug("Injury: ESPN athlete status fetch failed (id=%s): %s", athlete_id, exc)

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
        _INJURY_CACHE[cache_key] = (None, now)
        return None

    designation = _espn_injury_status(athlete_id, sport_slug, league_slug)

    if designation is not None:
        logger.info(
            "Injury status for %s (%s): %s", player_name, sport, designation
        )

    _INJURY_CACHE[cache_key] = (designation, now)
    return designation
