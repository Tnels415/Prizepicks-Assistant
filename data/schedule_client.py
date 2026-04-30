from __future__ import annotations

import logging
from datetime import date, datetime, timezone, timedelta

from nba_api.stats.static import teams as nba_teams_static

import requests

logger = logging.getLogger(__name__)

_TEAM_ABBR_TO_ID: dict[str, int] = {}
_TEAM_ID_TO_ABBR: dict[int, str] = {}


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
        """
        if sport_key == "basketball_nba":
            return self._get_nba_games()
        if sport_key == "icehockey_nhl":
            return self._get_nhl_games()
        if sport_key == "americanfootball_nfl":
            return self._get_nfl_games()
        if sport_key == "baseball_mlb":
            return self._get_mlb_games()
        logger.warning("ScheduleClient: unknown sport_key '%s'", sport_key)
        return []

    # ------------------------------------------------------------------
    # NBA
    # ------------------------------------------------------------------

    def _get_nba_games(self) -> list[dict]:
        games = self._try_nba_live_scoreboard()
        if not games:
            games = self._try_nba_stats_scoreboard()
        logger.info("Found %d NBA games today", len(games))
        return games

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
                })
            return results
        except Exception as exc:
            logger.warning("NBA stats scoreboard failed: %s", exc)
            return []

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

        games = []
        for event in data.get("events", []):
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
                params={"sportId": 1, "date": today_str, "hydrate": "team"},
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
