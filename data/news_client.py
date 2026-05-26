from __future__ import annotations

"""Sports news and analyst sentiment module.

Fetches recent ESPN news for players and computes a sentiment signal
(positive = player likely to exceed their line; negative = limited/struggling).
Results are cached in-memory for 60 minutes to avoid repeated API calls
within the same run.

The sentiment score is bounded to a small probability adjustment (±4pp) so
it acts as a tie-breaker, not a dominant factor.
"""

import logging
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

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

# ESPN API routes
_ESPN_SEARCH_URL = "https://site.api.espn.com/apis/common/v3/search"
_ESPN_ATHLETE_NEWS_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/athletes/{athlete_id}/news"
)

# Sport → ESPN sport/league slugs
_SPORT_ESPN_MAP: dict[str, tuple[str, str]] = {
    "NBA": ("basketball", "nba"),
    "NHL": ("hockey", "nhl"),
    "MLB": ("baseball", "mlb"),
    "NFL": ("football", "nfl"),
}

# Keywords associated with OVER performance (healthy, hot, productive)
_OVER_WORDS: list[str] = [
    "hot streak", "on fire", "dominant", "dominat", "stellar", "outstanding",
    "historic", "career high", "career-high", "record", "surge", "explosive",
    "strong performance", "healthy", "cleared", "full go", "full practice",
    "no limitations", "back in form", "back in lineup", "rolling",
    "playoff form", "playoff mode", "stepped up", "elevated",
]

# Keywords associated with UNDER performance (injury, limited, cold)
_UNDER_WORDS: list[str] = [
    "questionable", "doubtful", "probable", "injured", "injury", "hurt",
    "did not practice", "limited", "sidelined", "day-to-day", "out",
    "scratch", "scratched", "illness", "illness", "reaggravated",
    "sprain", "strain", "concussion", "shoulder", "knee", "ankle",
    "struggling", "slumping", "cold streak", "cold start", "resting",
    "load management", "managed", "reduced minutes", "reduced role",
]

# In-memory cache: (player_name, sport) → (timestamp, sentiment_float)
_NEWS_CACHE: dict[tuple[str, str], tuple[float, float]] = {}
_NEWS_CACHE_TTL = 60 * 60   # 1 hour

# Set True if ESPN is consistently failing this run
_ESPN_NEWS_UNAVAILABLE: bool = False


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
        logger.debug("ESPN athlete search failed for '%s': %s", player_name, exc)
    return None


def _sentiment_from_text(text: str) -> float:
    """Return a raw sentiment score from concatenated headline/description text.

    Score > 0: positive signals dominate (player trending well / healthy).
    Score < 0: negative signals dominate (injury / struggling).
    Score = 0: neutral or no signal found.
    Range: [-1.0, 1.0].
    """
    text_lower = text.lower()
    over_hits  = sum(1 for kw in _OVER_WORDS  if kw in text_lower)
    under_hits = sum(1 for kw in _UNDER_WORDS if kw in text_lower)
    total = over_hits + under_hits
    if total == 0:
        return 0.0
    return (over_hits - under_hits) / total


def get_player_news_sentiment(player_name: str, sport: str) -> float:
    """Fetch ESPN news for *player_name* and return a sentiment score [-1, 1].

    A score > 0 means recent context favours OVER; < 0 favours UNDER.
    Returns 0.0 on any fetch failure so the factor degrades gracefully.

    Results are cached for 60 minutes to avoid repeated API calls per run.
    """
    global _ESPN_NEWS_UNAVAILABLE
    if _ESPN_NEWS_UNAVAILABLE:
        return 0.0

    cache_key = (player_name.lower(), sport)
    now = time.time()
    cached = _NEWS_CACHE.get(cache_key)
    if cached is not None and (now - cached[0]) < _NEWS_CACHE_TTL:
        logger.debug("News cache hit for %s (%s): %.2f", player_name, sport, cached[1])
        return cached[1]

    slugs = _SPORT_ESPN_MAP.get(sport)
    if slugs is None:
        return 0.0
    sport_slug, league_slug = slugs

    athlete_id = _espn_athlete_id(player_name, sport_slug, league_slug)
    if athlete_id is None:
        _NEWS_CACHE[cache_key] = (now, 0.0)
        return 0.0

    try:
        url = _ESPN_ATHLETE_NEWS_URL.format(
            sport=sport_slug, league=league_slug, athlete_id=athlete_id
        )
        resp = requests.get(url, timeout=8)
        if resp.status_code == 404:
            _NEWS_CACHE[cache_key] = (now, 0.0)
            return 0.0
        resp.raise_for_status()
        articles = resp.json().get("articles", [])
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status and status >= 500:
            _ESPN_NEWS_UNAVAILABLE = True
            logger.warning("ESPN news API unavailable (HTTP %s) — disabling for this run", status)
        else:
            logger.debug("ESPN news fetch failed for '%s': %s", player_name, exc)
        _NEWS_CACHE[cache_key] = (now, 0.0)
        return 0.0

    if not articles:
        _NEWS_CACHE[cache_key] = (now, 0.0)
        return 0.0

    combined = " ".join(
        (a.get("headline", "") + " " + a.get("description", ""))
        for a in articles[:5]   # analyse up to 5 most recent articles
    )
    sentiment = _sentiment_from_text(combined)

    logger.info(
        "News sentiment for %s (%s): %.2f  (%d articles sampled)",
        player_name, sport, sentiment, min(len(articles), 5),
    )
    _NEWS_CACHE[cache_key] = (now, sentiment)
    return sentiment
