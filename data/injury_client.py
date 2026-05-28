from __future__ import annotations

"""Player injury designation client.

Fetches current injury/roster status from ESPN for all four supported sports.
Returns the official designation string (e.g. "Day-To-Day", "Questionable",
"Out") when a player has any active limitation, or None when the player is
healthy or status cannot be determined.

Primary strategy: bulk league injuries endpoint
  GET /apis/site/v2/sports/{sport}/{league}/injuries
  Returns all injured players for the entire league in one request.
  No per-player ID lookup needed. Uses the /site/v2/ path (not /common/v3/search
  which is frequently blocked). Cached per sport for 15 minutes.

Fallback strategy: per-player search + athlete detail
  GET /apis/common/v3/search → athlete ID
  GET /apis/site/v2/sports/{sport}/{league}/athletes/{id} → injury fields
  Used only when the bulk endpoint fails.

On any fetch failure the function returns None so downstream suppression is
skipped and the rest of the system is unaffected.
"""

import logging
import time

import requests

logger = logging.getLogger(__name__)

_ESPN_INJURIES_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/injuries"
)
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

# Per-player result cache: (name_lower, sport) → (designation | None, timestamp)
_INJURY_CACHE: dict[tuple[str, str], tuple[str | None, float]] = {}
_CACHE_TTL = 3600  # 1 hour

# Bulk league injury map: (sport_slug, league_slug) → ({name_lower: designation} | None, timestamp)
# None means fetch failed; {} means fetch succeeded with no injured players (off-season).
_BULK_CACHE: dict[tuple[str, str], tuple[dict[str, str] | None, float]] = {}
_BULK_CACHE_TTL = 900  # 15 minutes — short enough to pick up intra-day changes

_INJURY_API_UNAVAILABLE: bool = False


def _fetch_espn_injury_map(sport_slug: str, league_slug: str) -> dict[str, str] | None:
    """Fetch all current league injuries from ESPN in one bulk request.

    Returns {player_name_lower: designation} on success, or None on failure.
    An empty dict means the request succeeded but no players are on the report
    (valid in the off-season). None means the request itself failed.
    """
    global _INJURY_API_UNAVAILABLE
    if _INJURY_API_UNAVAILABLE:
        return None

    cache_key = (sport_slug, league_slug)
    now = time.time()
    cached = _BULK_CACHE.get(cache_key)
    if cached is not None and (now - cached[1]) < _BULK_CACHE_TTL:
        return cached[0]

    try:
        url = _ESPN_INJURIES_URL.format(sport=sport_slug, league=league_slug)
        resp = requests.get(url, headers=_ESPN_HEADERS, timeout=12)
        resp.raise_for_status()
        data = resp.json()

        injury_map: dict[str, str] = {}
        for team_block in data.get("injuries", []):
            for injury in team_block.get("injuries", []):
                athlete = injury.get("athlete", {})
                name = athlete.get("displayName", "").strip()
                if not name:
                    continue
                # Designation comes from the injury-level status, not the athlete object
                designation = (
                    injury.get("status")
                    or injury.get("type")
                    or athlete.get("injuryStatus")
                    or "Injured"
                )
                injury_map[name.lower()] = str(designation)

        logger.info(
            "ESPN bulk injuries (%s/%s): %d player(s) on injury report",
            sport_slug, league_slug, len(injury_map),
        )
        _BULK_CACHE[cache_key] = (injury_map, now)
        return injury_map

    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code and status_code >= 500:
            _INJURY_API_UNAVAILABLE = True
            logger.warning(
                "ESPN injuries API HTTP %s — disabling for this run", status_code
            )
        else:
            logger.info(
                "ESPN bulk injuries fetch failed (%s/%s): %s", sport_slug, league_slug, exc
            )
        # Cache the failure briefly so we don't hammer on every prop check
        _BULK_CACHE[cache_key] = (None, now)
        return None


def _espn_athlete_id(player_name: str, sport_slug: str, league_slug: str) -> str | None:
    """Return ESPN athlete ID for a player via search, or None on failure."""
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
    the player has an active limitation and their prop should be suppressed.

    Strategy:
    1. Check per-player cache (1-hour TTL).
    2. Try ESPN bulk league injuries — one call covers all players for this sport.
       If the map was fetched successfully and the player is NOT in it, they are
       healthy (return None and cache that result).
    3. Fall back to per-player search + athlete detail if bulk fails.
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

    # --- Primary: bulk league injury map (no per-player search needed) ---
    injury_map = _fetch_espn_injury_map(sport_slug, league_slug)
    if injury_map is not None:
        # Bulk fetch succeeded — the map is authoritative for this sport
        designation = injury_map.get(player_name.lower())
        if designation is not None:
            logger.info(
                "Injury status for %s (%s): %s", player_name, sport, designation
            )
        _INJURY_CACHE[cache_key] = (designation, now)
        return designation

    # --- Fallback: per-player search + athlete detail ---
    athlete_id = _espn_athlete_id(player_name, sport_slug, league_slug)
    if athlete_id is None:
        # Don't cache — search failure is transient; retry on the next prop
        return None

    designation = _espn_injury_status(athlete_id, sport_slug, league_slug)

    if designation is not None:
        logger.info(
            "Injury status for %s (%s): %s [fallback]", player_name, sport, designation
        )

    _INJURY_CACHE[cache_key] = (designation, now)
    return designation
