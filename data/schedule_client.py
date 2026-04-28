import logging
from datetime import date, datetime, timedelta

from nba_api.stats.static import teams as nba_teams_static

logger = logging.getLogger(__name__)

_TEAM_ABBR_TO_ID: dict[str, int] = {}
_TEAM_ID_TO_ABBR: dict[int, str] = {}


def _build_team_maps():
    global _TEAM_ABBR_TO_ID, _TEAM_ID_TO_ABBR
    if _TEAM_ABBR_TO_ID:
        return
    for t in nba_teams_static.get_teams():
        _TEAM_ABBR_TO_ID[t["abbreviation"]] = t["id"]
        _TEAM_ID_TO_ABBR[t["id"]] = t["abbreviation"]


class ScheduleClient:

    def __init__(self):
        _build_team_maps()

    def get_todays_games(self) -> list[dict]:
        games = self._try_live_scoreboard()
        if not games:
            games = self._try_stats_scoreboard()
        logger.info("Found %d NBA games today", len(games))
        return games

    def _try_live_scoreboard(self) -> list[dict]:
        try:
            from nba_api.live.nba.endpoints import scoreboard as live_sb
            sb = live_sb.ScoreBoard()
            raw = sb.get_dict()
            game_list = raw.get("scoreboard", {}).get("games", [])
            return [self._parse_live_game(g) for g in game_list if self._parse_live_game(g)]
        except Exception as exc:
            logger.debug("Live scoreboard failed: %s", exc)
            return []

    def _try_stats_scoreboard(self) -> list[dict]:
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
            logger.warning("Stats scoreboard failed: %s", exc)
            return []

    def _parse_live_game(self, game: dict) -> dict | None:
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

    def get_team_abbr_to_id(self) -> dict[str, int]:
        return dict(_TEAM_ABBR_TO_ID)

    def get_team_id_to_abbr(self) -> dict[int, str]:
        return dict(_TEAM_ID_TO_ABBR)
