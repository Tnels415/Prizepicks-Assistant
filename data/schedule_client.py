from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone, timedelta

from nba_api.stats.static import teams as nba_teams_static

import requests

logger = logging.getLogger(__name__)

_TEAM_ABBR_TO_ID: dict[str, int] = {}
_TEAM_ID_TO_ABBR: dict[int, str] = {}

# Per-sport schedule cache keyed by sport_key; reset when the date changes.
_SCHEDULE_CACHE: dict[str, list[dict]] = {}
_SCHEDULE_CACHE_DATE: date | None = None


def _parse_espn_broadcasts(comp: dict) -> list[dict]:
    """Extract TV broadcasts from an ESPN competition dict.

    Prefers the richer geoBroadcasts[] array; falls back to broadcasts[].
    Returns [{"network": str, "market": "national"|"home"|"away"}].
    """
    out: list[dict] = []
    geo = comp.get("geoBroadcasts") or []
    for g in geo:
        # Only TV feeds (skip Radio / Streaming-only labeled non-TV).
        if str(g.get("type", {}).get("shortName", "")).upper() not in ("TV", ""):
            continue
        network = (g.get("media", {}) or {}).get("shortName", "")
        market = str((g.get("market", {}) or {}).get("type", "")).lower()  # National/Home/Away
        if network:
            out.append({"network": network, "market": market or "national"})
    if out:
        return out
    # Fallback: coarse broadcasts[] (names + market string).
    for b in comp.get("broadcasts") or []:
        market = str(b.get("market", "")).lower()
        for name in b.get("names", []) or []:
            if name:
                out.append({"network": name, "market": market or "national"})
    return out


_NHL_MARKET_MAP = {"N": "national", "H": "home", "A": "away"}


def _parse_nhl_broadcasts(game: dict) -> list[dict]:
    """Extract US TV broadcasts from an NHL game dict's tvBroadcasts[]."""
    out: list[dict] = []
    for b in game.get("tvBroadcasts") or []:
        if str(b.get("countryCode", "US")).upper() != "US":
            continue
        network = b.get("network", "")
        market = _NHL_MARKET_MAP.get(str(b.get("market", "")).upper(), "national")
        if network:
            out.append({"network": network, "market": market})
    return out


def _parse_mlb_broadcasts(game: dict) -> list[dict]:
    """Extract TV broadcasts from an MLB game dict's broadcasts[] (broadcasts(all) hydrate)."""
    out: list[dict] = []
    for b in game.get("broadcasts") or []:
        if str(b.get("type", "")).upper() != "TV":
            continue
        network = b.get("name") or b.get("callSign") or ""
        if b.get("isNational"):
            market = "national"
        else:
            market = str(b.get("homeAway", "")).lower() or "home"
        if network:
            out.append({"network": network, "market": market})
    return out


# Odds API returns full team names; map them to standard NBA abbreviations.
_TEAM_NAME_TO_ABBR: dict[str, str] = {
    "Atlanta Hawks": "ATL", "Boston Celtics": "BOS", "Brooklyn Nets": "BKN",
    "Charlotte Hornets": "CHA", "Chicago Bulls": "CHI", "Cleveland Cavaliers": "CLE",
    "Dallas Mavericks": "DAL", "Denver Nuggets": "DEN", "Detroit Pistons": "DET",
    "Golden State Warriors": "GSW", "Houston Rockets": "HOU", "Indiana Pacers": "IND",
    "LA Clippers": "LAC", "Los Angeles Clippers": "LAC", "Los Angeles Lakers": "LAL",
    "Memphis Grizzlies": "MEM", "Miami Heat": "MIA", "Milwaukee Bucks": "MIL",
    "Minnesota Timberwolves": "MIN", "New Orleans Pelicans": "NOP", "New York Knicks": "NYK",
    "Oklahoma City Thunder": "OKC", "Orlando Magic": "ORL", "Philadelphia 76ers": "PHI",
    "Phoenix Suns": "PHX", "Portland Trail Blazers": "POR", "Sacramento Kings": "SAC",
    "San Antonio Spurs": "SAS", "Toronto Raptors": "TOR", "Utah Jazz": "UTA",
    "Washington Wizards": "WAS",
}


def _build_nba_team_maps() -> None:
    global _TEAM_ABBR_TO_ID, _TEAM_ID_TO_ABBR
    if _TEAM_ABBR_TO_ID:
        return
    for t in nba_teams_static.get_teams():
        _TEAM_ABBR_TO_ID[t["abbreviation"]] = t["id"]
        _TEAM_ID_TO_ABBR[t["id"]] = t["abbreviation"]


class ScheduleClient:

    def __init__(self) -> None:
        _build_nba_team_maps()

    # ------------------------------------------------------------------
    # Unified entry point — dispatches by sport key
    # ------------------------------------------------------------------

    def get_todays_games(self, sport_key: str = "basketball_nba") -> list[dict]:
        """Return today's games for the given sport. Game dicts have keys:
            game_id, home_team_abbr, home_team_id, away_team_abbr, away_team_id, game_status

        Results are cached for the calendar day so repeated calls (e.g. from
        main.py and then from PropAnalyzer) hit the network only once.
        """
        global _SCHEDULE_CACHE, _SCHEDULE_CACHE_DATE
        today = date.today()
        if _SCHEDULE_CACHE_DATE != today:
            _SCHEDULE_CACHE = {}
            _SCHEDULE_CACHE_DATE = today
        if sport_key in _SCHEDULE_CACHE:
            return _SCHEDULE_CACHE[sport_key]

        if sport_key == "basketball_nba":
            games = self._get_nba_games()
        elif sport_key == "icehockey_nhl":
            games = self._get_nhl_games()
        elif sport_key == "americanfootball_nfl":
            games = self._get_nfl_games()
        elif sport_key == "baseball_mlb":
            games = self._get_mlb_games()
        else:
            logger.warning("ScheduleClient: unknown sport_key '%s'", sport_key)
            games = []

        _SCHEDULE_CACHE[sport_key] = games
        return games

    # ------------------------------------------------------------------
    # NBA
    # ------------------------------------------------------------------

    def _get_nba_games(self) -> list[dict]:
        games = self._try_nba_live_scoreboard()
        if not games:
            games = self._try_nba_stats_scoreboard()
        if not games:
            games = self._try_espn_nba_scoreboard()
        if not games:
            games = self._try_odds_api_nba_events()
        if not games:
            games = self._infer_nba_games_from_props_json()
        # The official NBA scoreboards carry no TV data — enrich from ESPN so the
        # "Watchable on TV" view works regardless of which source succeeded.
        if games and not any(g.get("broadcasts") for g in games):
            self._enrich_nba_broadcasts(games)
        logger.info("Found %d NBA games today", len(games))
        return games

    def _enrich_nba_broadcasts(self, games: list[dict]) -> None:
        """Fill empty broadcasts on NBA games using the ESPN scoreboard, matched by team abbr."""
        try:
            resp = requests.get(
                "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("NBA broadcast enrichment skipped (ESPN fetch failed): %s", exc)
            return

        today_str = date.today().isoformat()
        by_abbr: dict[str, list[dict]] = {}
        for event in data.get("events", []):
            if (event.get("date", "") or "")[:10] != today_str:
                continue
            for comp in event.get("competitions", [{}]):
                bcasts = _parse_espn_broadcasts(comp)
                if not bcasts:
                    continue
                for c in comp.get("competitors", []):
                    raw = c.get("team", {}).get("abbreviation", "")
                    abbr = self._ESPN_NBA_ABBR_MAP.get(raw, raw).upper()
                    if abbr:
                        by_abbr[abbr] = bcasts

        enriched = 0
        for g in games:
            if g.get("broadcasts"):
                continue
            bcasts = (
                by_abbr.get(g.get("home_team_abbr", "").upper())
                or by_abbr.get(g.get("away_team_abbr", "").upper())
                or []
            )
            g["broadcasts"] = bcasts
            if bcasts:
                enriched += 1

        logger.info(
            "NBA broadcast enrichment: %d/%d games matched to ESPN broadcast data "
            "(ESPN abbr map has %d entries)",
            enriched, len(games), len(by_abbr),
        )

    def _try_nba_live_scoreboard(self) -> list[dict]:
        try:
            from nba_api.live.nba.endpoints import scoreboard as live_sb
            sb = live_sb.ScoreBoard()
            raw = sb.get_dict()
            game_list = raw.get("scoreboard", {}).get("games", [])
            return [g for g in (self._parse_nba_live_game(x) for x in game_list) if g]
        except Exception as exc:
            logger.debug("NBA live scoreboard failed: %s", exc)
            return []

    def _try_nba_stats_scoreboard(self) -> list[dict]:
        try:
            from nba_api.stats.endpoints import scoreboardv2
            today_str = date.today().strftime("%m/%d/%Y")
            sb = scoreboardv2.ScoreboardV2(game_date=today_str, league_id="00", day_offset=0)
            df = sb.game_header.get_data_frame()
            results = []
            for _, row in df.iterrows():
                home_id = int(row["HOME_TEAM_ID"])
                away_id = int(row["VISITOR_TEAM_ID"])
                results.append({
                    "game_id": str(row["GAME_ID"]),
                    "home_team_id": home_id,
                    "home_team_abbr": _TEAM_ID_TO_ABBR.get(home_id, ""),
                    "away_team_id": away_id,
                    "away_team_abbr": _TEAM_ID_TO_ABBR.get(away_id, ""),
                    "game_status": str(row.get("GAME_STATUS_TEXT", "")),
                    "broadcasts": [],
                })
            return results
        except Exception as exc:
            logger.warning("NBA stats scoreboard failed: %s", exc)
            return []

    # ESPN uses slightly different abbreviations for some NBA teams.
    # Normalize them to the NBA-standard abbreviations used by _TEAM_ABBR_TO_ID.
    _ESPN_NBA_ABBR_MAP: dict[str, str] = {
        "GS": "GSW",   # Golden State Warriors
        "SA": "SAS",   # San Antonio Spurs
        "NY": "NYK",   # New York Knicks
        "NO": "NOP",   # New Orleans Pelicans
        "UTAH": "UTA", # Utah Jazz
        "WSH": "WAS",  # Washington Wizards
    }

    def _try_espn_nba_scoreboard(self) -> list[dict]:
        """ESPN unofficial scoreboard — used as a fallback when stats.nba.com is unavailable."""
        try:
            resp = requests.get(
                "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("ESPN NBA scoreboard failed: %s", exc)
            return []

        today_str = date.today().isoformat()  # "YYYY-MM-DD"
        games = []
        for event in data.get("events", []):
            # ESPN returns a multi-day window; only keep today's games.
            event_date = event.get("date", "")  # ISO-8601, e.g. "2025-06-01T17:00Z"
            if event_date and event_date[:10] != today_str:
                continue
            for comp in event.get("competitions", [{}]):
                competitors = comp.get("competitors", [])
                home = next((c for c in competitors if c.get("homeAway") == "home"), {})
                away = next((c for c in competitors if c.get("homeAway") == "away"), {})
                home_team = home.get("team", {})
                away_team = away.get("team", {})
                if not home_team or not away_team:
                    continue
                home_abbr = self._ESPN_NBA_ABBR_MAP.get(
                    home_team.get("abbreviation", ""),
                    home_team.get("abbreviation", ""),
                )
                away_abbr = self._ESPN_NBA_ABBR_MAP.get(
                    away_team.get("abbreviation", ""),
                    away_team.get("abbreviation", ""),
                )
                # Map abbreviations to NBA team IDs so team-stats lookups work correctly.
                home_id = _TEAM_ABBR_TO_ID.get(home_abbr, 0)
                away_id = _TEAM_ABBR_TO_ID.get(away_abbr, 0)
                games.append({
                    "game_id": str(event.get("id", "")),
                    "home_team_id": home_id,
                    "home_team_abbr": home_abbr,
                    "away_team_id": away_id,
                    "away_team_abbr": away_abbr,
                    "game_status": str(
                        event.get("status", {}).get("type", {}).get("name", "")
                    ),
                    "broadcasts": _parse_espn_broadcasts(comp),
                })

        logger.info("ESPN NBA fallback: found %d games today", len(games))
        return games

    def _try_odds_api_nba_events(self) -> list[dict]:
        """Odds API v4 events endpoint — last-resort fallback when all other NBA
        schedule sources are unavailable (e.g. ESPN 403, stats.nba.com down).
        Returns minimal game dicts (no team IDs — stat lookups will still work
        via player-name matching in the analyzer).
        """
        # Use load_config() so the key is resolved from .env just like other clients.
        try:
            from config import load_config
            api_key = load_config().get("odds_api_key") or os.environ.get("THE_ODDS_API_KEY", "")
        except Exception:
            api_key = os.environ.get("THE_ODDS_API_KEY", "")
        if not api_key:
            logger.debug("Odds API key not set — skipping Odds API NBA events fallback")
            return []
        try:
            resp = requests.get(
                "https://api.the-odds-api.com/v4/sports/basketball_nba/events",
                params={"apiKey": api_key, "dateFormat": "iso"},
                timeout=15,
            )
            resp.raise_for_status()
            events = resp.json()
        except Exception as exc:
            logger.warning("Odds API NBA events fallback failed: %s", exc)
            return []

        today_str = date.today().isoformat()
        games = []
        for ev in events:
            # commence_time is ISO-8601; filter to today (UTC date may differ near midnight)
            ct = ev.get("commence_time", "")
            if not ct or ct[:10] != today_str:
                continue
            home_raw = ev.get("home_team", "")
            away_raw = ev.get("away_team", "")
            # Map full team names to abbreviations via existing map
            home_abbr = _TEAM_NAME_TO_ABBR.get(home_raw, home_raw[:3].upper())
            away_abbr = _TEAM_NAME_TO_ABBR.get(away_raw, away_raw[:3].upper())
            home_id = _TEAM_ABBR_TO_ID.get(home_abbr, 0)
            away_id = _TEAM_ABBR_TO_ID.get(away_abbr, 0)
            games.append({
                "game_id": str(ev.get("id", "")),
                "home_team_id": home_id,
                "home_team_abbr": home_abbr,
                "away_team_id": away_id,
                "away_team_abbr": away_abbr,
                "game_status": "scheduled",
                "broadcasts": [],
            })

        logger.info("Odds API NBA events fallback: found %d games today", len(games))
        return games

    def _infer_nba_games_from_props_json(self) -> list[dict]:
        """Absolute last resort: read team abbreviations from props.json and create
        placeholder game entries so the analyzer can proceed when all schedule APIs
        are unavailable (e.g. blocked network or post-season API changes).

        Pairs teams by splitting the unique set into pairs (works cleanly for
        2-team playoff matchups; for multi-game nights each unique team gets a
        entry so _determine_game_context can find it).
        """
        import json
        from pathlib import Path

        props_file = Path("props.json")
        if not props_file.exists():
            return []
        try:
            raw = json.loads(props_file.read_text(encoding="utf-8"))
        except Exception:
            return []

        if not isinstance(raw, list):
            return []

        # Collect unique NBA team abbreviations from non-template entries.
        template_names = {"Jayson Tatum", "Connor McDavid", "Shohei Ohtani"}
        nba_teams: list[str] = []
        for entry in raw:
            if str(entry.get("sport", "")).upper() != "NBA":
                continue
            if entry.get("player_name") in template_names:
                continue
            abbr = str(entry.get("team_abbr", "")).upper()
            if abbr and abbr not in nba_teams:
                nba_teams.append(abbr)

        if not nba_teams:
            logger.debug("props.json has no real NBA entries — cannot infer games")
            return []

        # Pair teams: [A, B, C, D] → (A,B), (C,D); odd team gets empty opponent.
        games: list[dict] = []
        for i in range(0, len(nba_teams), 2):
            home = nba_teams[i]
            away = nba_teams[i + 1] if i + 1 < len(nba_teams) else ""
            home_id = _TEAM_ABBR_TO_ID.get(home, 0)
            away_id = _TEAM_ABBR_TO_ID.get(away, 0) if away else 0
            games.append({
                "game_id": f"inferred_{home}_{away}",
                "home_team_id": home_id,
                "home_team_abbr": home,
                "away_team_id": away_id,
                "away_team_abbr": away,
                "game_status": "inferred_from_props",
                "broadcasts": [],
            })

        logger.warning(
            "All NBA schedule APIs unavailable — inferred %d game(s) from props.json "
            "team abbreviations (%s). Opponent-defense factor will be 0 for inferred "
            "matchups. Fill props.json with real PrizePicks lines.",
            len(games), ", ".join(nba_teams),
        )
        return games

    def _parse_nba_live_game(self, game: dict) -> dict | None:
        try:
            home = game.get("homeTeam", {})
            away = game.get("awayTeam", {})
            home_id = int(home.get("teamId", 0))
            away_id = int(away.get("teamId", 0))
            if not home_id or not away_id:
                return None
            return {
                "game_id": str(game.get("gameId", "")),
                "home_team_id": home_id,
                "home_team_abbr": home.get("teamTricode", _TEAM_ID_TO_ABBR.get(home_id, "")),
                "away_team_id": away_id,
                "away_team_abbr": away.get("teamTricode", _TEAM_ID_TO_ABBR.get(away_id, "")),
                "broadcasts": [],
                "game_status": str(game.get("gameStatus", "")),
            }
        except Exception:
            return None

    # ------------------------------------------------------------------
    # NHL — official NHL Web API (api-web.nhle.com)
    # ------------------------------------------------------------------

    def _get_nhl_games(self) -> list[dict]:
        today_str = date.today().isoformat()
        try:
            resp = requests.get(
                f"https://api-web.nhle.com/v1/schedule/{today_str}",
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("NHL schedule fetch failed: %s", exc)
            return []

        games = []
        for day in data.get("gameWeek", []):
            if day.get("date") != today_str:
                continue
            for g in day.get("games", []):
                home = g.get("homeTeam", {})
                away = g.get("awayTeam", {})
                games.append({
                    "game_id": str(g.get("id", "")),
                    "home_team_id": int(home.get("id", 0)),
                    "home_team_abbr": home.get("abbrev", ""),
                    "away_team_id": int(away.get("id", 0)),
                    "away_team_abbr": away.get("abbrev", ""),
                    "game_status": str(g.get("gameState", "")),
                    "broadcasts": _parse_nhl_broadcasts(g),
                })

        logger.info("Found %d NHL games today", len(games))
        return games

    # ------------------------------------------------------------------
    # NFL — ESPN unofficial scoreboard API
    # ------------------------------------------------------------------

    def _get_nfl_games(self) -> list[dict]:
        try:
            resp = requests.get(
                "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("NFL schedule fetch failed: %s", exc)
            return []

        today_str = date.today().isoformat()  # "YYYY-MM-DD"
        games = []
        for event in data.get("events", []):
            # ESPN returns a multi-day window; only keep today's games.
            event_date = event.get("date", "")  # ISO-8601, e.g. "2025-09-07T17:00Z"
            if event_date and event_date[:10] != today_str:
                continue
            for comp in event.get("competitions", [{}]):
                competitors = comp.get("competitors", [])
                home = next((c for c in competitors if c.get("homeAway") == "home"), {})
                away = next((c for c in competitors if c.get("homeAway") == "away"), {})
                home_team = home.get("team", {})
                away_team = away.get("team", {})
                if not home_team or not away_team:
                    continue
                games.append({
                    "game_id": str(event.get("id", "")),
                    "home_team_id": int(home_team.get("id", 0)),
                    "home_team_abbr": home_team.get("abbreviation", ""),
                    "away_team_id": int(away_team.get("id", 0)),
                    "away_team_abbr": away_team.get("abbreviation", ""),
                    "game_status": str(event.get("status", {}).get("type", {}).get("name", "")),
                    "broadcasts": _parse_espn_broadcasts(comp),
                })

        logger.info("Found %d NFL games today", len(games))
        return games

    # ------------------------------------------------------------------
    # MLB — official MLB Stats API
    # ------------------------------------------------------------------

    def _get_mlb_games(self) -> list[dict]:
        today_str = date.today().isoformat()
        try:
            resp = requests.get(
                "https://statsapi.mlb.com/api/v1/schedule",
                params={"sportId": 1, "date": today_str, "hydrate": "team,broadcasts(all)"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("MLB schedule fetch failed: %s", exc)
            return []

        games = []
        for day in data.get("dates", []):
            for g in day.get("games", []):
                teams = g.get("teams", {})
                home = teams.get("home", {}).get("team", {})
                away = teams.get("away", {}).get("team", {})
                games.append({
                    "game_id": str(g.get("gamePk", "")),
                    "home_team_id": int(home.get("id", 0)),
                    "home_team_abbr": home.get("abbreviation", ""),
                    "away_team_id": int(away.get("id", 0)),
                    "away_team_abbr": away.get("abbreviation", ""),
                    "game_status": str(g.get("status", {}).get("abstractGameState", "")),
                    "broadcasts": _parse_mlb_broadcasts(g),
                })

        logger.info("Found %d MLB games today", len(games))
        return games

    # ------------------------------------------------------------------
    # NBA team-id helpers (used by PropAnalyzer for pace scoring)
    # ------------------------------------------------------------------

    def get_team_abbr_to_id(self) -> dict[str, int]:
        return dict(_TEAM_ABBR_TO_ID)

    def get_team_id_to_abbr(self) -> dict[int, str]:
        return dict(_TEAM_ID_TO_ABBR)
