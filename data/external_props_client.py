from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import date

logger = logging.getLogger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _fetch_via_system_curl(url: str, params: dict, headers: dict) -> bytes | None:
    """Fetch url using the system `curl` binary (native TLS stack)."""
    curl_path = shutil.which("curl")
    if not curl_path:
        return None

    qs = "&".join(f"{k}={v}" for k, v in params.items())
    full_url = f"{url}?{qs}" if qs else url

    cmd = [curl_path, "-s", "--max-time", "25", "--compressed", full_url]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    cmd += ["-w", "\n__STATUS__:%{http_code}"]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        output = result.stdout.decode("utf-8", errors="replace")
        if "\n__STATUS__:" in output:
            body, status_str = output.rsplit("\n__STATUS__:", 1)
            status = int(status_str.strip())
        else:
            body = output
            status = 0
        if status == 200:
            return body.encode("utf-8")
        logger.debug("system curl returned HTTP %d for %s", status, url)
        return None
    except Exception as exc:
        logger.debug("system curl failed for %s: %s", url, exc)
        return None


def _fetch_json(url: str, params: dict, headers: dict) -> dict | None:
    """Try system curl first, then requests. Return parsed JSON or None."""
    raw = _fetch_via_system_curl(url, params, headers)
    if raw is not None:
        try:
            return json.loads(raw)
        except Exception as exc:
            logger.warning("JSON parse error from %s: %s", url, exc)

    try:
        import requests as _requests
        resp = _requests.get(url, params=params, headers=headers, timeout=20)
        if resp.status_code == 403:
            logger.warning("HTTP 403 from %s — bot protection may be blocking the request", url)
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("Fetch failed for %s: %s", url, exc)
    return None


# Underdog stat names that map to our internal stat_type strings.
# Entries mapped to None are skipped.
UD_STAT_MAP: dict[str, str | None] = {
    # NBA
    "Points": "Points",
    "Rebounds": "Rebounds",
    "Assists": "Assists",
    "3-Pointers Made": "3-PT Made",
    "3-PT Made": "3-PT Made",
    "Steals": "Steals",
    "Blocks": "Blocks",
    "Turnovers": "Turnovers",
    "Points + Rebounds + Assists": "Pts+Reb+Ast",
    "Points + Assists": "Pts+Ast",
    "Points + Rebounds": "Pts+Reb",
    "Rebounds + Assists": "Reb+Ast",
    # NHL
    "Goals": "Goals",
    "Shots on Goal": "Shots on Goal",
    "Power Play Points": "Power Play Points",
    # MLB
    "Hits": "Hits",
    "Home Runs": "Home Runs",
    "RBIs": "RBIs",
    "Total Bases": "Total Bases",
    "Runs Scored": "Runs Scored",
    "Stolen Bases": "Stolen Bases",
    "Strikeouts": "Strikeouts",
    # NFL
    "Passing Yards": "Passing Yards",
    "Rushing Yards": "Rushing Yards",
    "Receiving Yards": "Receiving Yards",
    "Receptions": "Receptions",
    "Passing TDs": "Passing TDs",
    # Skip
    "Fantasy Points": None,
    "Fantasy Score": None,
    "Winning Margin": None,
}

_UD_SPORT_MAP = {
    "NBA": "NBA",
    "NHL": "NHL",
    "MLB": "MLB",
    "NFL": "NFL",
}

_UD_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


class UnderdogPropsClient:
    """Fetches player prop lines from Underdog Fantasy public API."""

    _URL = "https://api.underdogfantasy.com/beta/v5/over_under_lines"

    def fetch_props(self, sport_config: dict) -> list[dict]:
        sport_name = sport_config["name"]
        prop_stat_map = sport_config["prop_stat_map"]
        ud_sport = _UD_SPORT_MAP.get(sport_name)
        if not ud_sport:
            return []

        try:
            data = _fetch_json(self._URL, {}, _UD_HEADERS)
        except Exception as exc:
            logger.warning("Underdog fetch error: %s", exc)
            return []

        if not data:
            return []

        try:
            return self._parse(data, sport_name, ud_sport, prop_stat_map)
        except Exception as exc:
            logger.warning("Underdog parse error: %s", exc)
            return []

    def _parse(
        self,
        data: dict,
        sport_name: str,
        ud_sport: str,
        prop_stat_map: dict,
    ) -> list[dict]:
        today = date.today()

        # Build lookup dicts
        over_unders: dict[str, dict] = {}
        for ou in data.get("over_unders", []):
            try:
                over_unders[ou["id"]] = ou
            except Exception:
                continue

        appearance_stats: dict[str, dict] = {}
        for astat in data.get("appearance_stats", []):
            try:
                appearance_stats[astat["id"]] = astat
            except Exception:
                continue

        appearances: dict[str, dict] = {}
        for ap in data.get("appearances", []):
            try:
                appearances[ap["id"]] = ap
            except Exception:
                continue

        players: dict[str, dict] = {}
        for pl in data.get("players", []):
            try:
                players[pl["id"]] = pl
            except Exception:
                continue

        match_ups: dict[str, dict] = {}
        for mu in data.get("match_ups", []):
            try:
                match_ups[mu["id"]] = mu
            except Exception:
                continue

        seen: dict[tuple, dict] = {}

        for line in data.get("over_under_lines", []):
            try:
                stat_value = line.get("stat_value")
                if stat_value is None:
                    continue
                ou_id = line.get("over_under_id")
                if not ou_id:
                    continue

                ou = over_unders.get(ou_id)
                if not ou:
                    continue

                astat_id = ou.get("appearance_stat_id")
                if not astat_id:
                    continue

                astat = appearance_stats.get(astat_id)
                if not astat:
                    continue

                raw_stat = astat.get("stat", "")
                stat_type = UD_STAT_MAP.get(raw_stat, raw_stat)
                if stat_type is None:
                    continue
                if stat_type not in prop_stat_map:
                    continue

                appearance_id = astat.get("appearance_id")
                if not appearance_id:
                    continue
                appearance = appearances.get(appearance_id)
                if not appearance:
                    continue

                match_up_id = appearance.get("match_up_id")
                match_up = match_ups.get(match_up_id or "")
                if not match_up:
                    continue

                mu_sport = match_up.get("sport", "")
                if mu_sport.upper() != ud_sport:
                    continue

                player_id = appearance.get("player_id")
                player = players.get(player_id or "")
                if not player:
                    continue

                player_name = f"{player.get('first_name', '')} {player.get('last_name', '')}".strip()
                if not player_name:
                    continue

                team_name = appearance.get("team_name", "")
                position = appearance.get("position_abbreviation", "")

                key = (player_name, stat_type)
                if key not in seen:
                    seen[key] = {
                        "sport": sport_name,
                        "projection_id": str(line.get("id", len(seen))),
                        "player_name": player_name,
                        "team_abbr": team_name,
                        "event_home_abbr": match_up.get("home_team_name", ""),
                        "event_away_abbr": match_up.get("away_team_name", ""),
                        "position": position,
                        "stat_type": stat_type,
                        "line": float(stat_value),
                        "start_time": "",
                        "pick_type": "standard",
                        "n_variants": 1,
                        "from_multiple_lines": False,
                        "prop_source": "Underdog",
                    }
            except Exception:
                continue

        props = list(seen.values())
        logger.info("Underdog: %d %s props parsed", len(props), sport_name)
        return props


# FanDuel sport path mapping
FD_SPORT_PATHS: dict[str, str] = {
    "NBA": "basketball",
    "MLB": "baseball",
    "NHL": "ice-hockey",
    "NFL": "american-football",
}

FD_STAT_MAP: dict[str, str] = {
    # NBA
    "Player Points": "Points",
    "Player Rebounds": "Rebounds",
    "Player Assists": "Assists",
    "Player 3s Made": "3-PT Made",
    "Player 3-Pointers Made": "3-PT Made",
    "Player Steals": "Steals",
    "Player Blocks": "Blocks",
    "Player Turnovers": "Turnovers",
    "Player Points + Rebounds + Assists": "Pts+Reb+Ast",
    "Player Points + Assists": "Pts+Ast",
    "Player Points + Rebounds": "Pts+Reb",
    "Player Rebounds + Assists": "Reb+Ast",
    # NHL
    "Player Goals": "Goals",
    "Player Points": "Points",
    "Player Assists": "Assists",
    "Player Shots on Goal": "Shots on Goal",
    "Player Power Play Points": "Power Play Points",
    # MLB
    "Player Hits": "Hits",
    "Player Home Runs": "Home Runs",
    "Player RBIs": "RBIs",
    "Player Total Bases": "Total Bases",
    "Player Runs Scored": "Runs Scored",
    "Player Stolen Bases": "Stolen Bases",
    "Batter Strikeouts": "Strikeouts",
    "Pitcher Strikeouts": "Strikeouts",
    # NFL
    "Player Passing Yards": "Passing Yards",
    "Player Rushing Yards": "Rushing Yards",
    "Player Receiving Yards": "Receiving Yards",
    "Player Receptions": "Receptions",
    "Player Passing TDs": "Passing TDs",
}

_FD_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.fanduel.com/",
}


class FanDuelPropsClient:
    """Fetches player prop lines from FanDuel public sportsbook API."""

    _BASE_URL = "https://sbapi.fanduel.com/api/sports/{sport_path}/competitions/all/markets"
    _AK = "FhMFpcPWXMeyZxOx"

    def fetch_props(self, sport_config: dict) -> list[dict]:
        sport_name = sport_config["name"]
        prop_stat_map = sport_config["prop_stat_map"]
        sport_path = FD_SPORT_PATHS.get(sport_name)
        if not sport_path:
            return []

        url = self._BASE_URL.format(sport_path=sport_path)
        params = {
            "_ak": self._AK,
            "betTypes": "PLAYER_SPECIAL",
            "jurisdiction": "US",
        }

        try:
            data = _fetch_json(url, params, _FD_HEADERS)
        except Exception as exc:
            logger.warning("FanDuel fetch error for %s: %s", sport_name, exc)
            return []

        if not data:
            return []

        try:
            return self._parse(data, sport_name, prop_stat_map)
        except Exception as exc:
            logger.warning("FanDuel parse error for %s: %s", sport_name, exc)
            return []

    def _parse(self, data: dict, sport_name: str, prop_stat_map: dict) -> list[dict]:
        attachments = data.get("attachments", {})
        markets_raw = attachments.get("markets", {})
        runners_raw = attachments.get("runners", {})

        seen: dict[tuple, dict] = {}

        for market_id, market in markets_raw.items():
            try:
                market_name = market.get("name", "")
                stat_type = FD_STAT_MAP.get(market_name, market_name)
                if stat_type not in prop_stat_map:
                    continue
                if prop_stat_map.get(stat_type) is None:
                    continue

                runner_ids = market.get("runners", [])
                for runner_id in runner_ids:
                    try:
                        runner = runners_raw.get(str(runner_id), {})
                        runner_name = runner.get("runnerName", "")
                        handicap = runner.get("handicap")
                        if handicap is None or not runner_name:
                            continue

                        # runnerName format: "Jayson Tatum Over 27.5"
                        # Extract player name (everything before Over/Under)
                        lower_rn = runner_name.lower()
                        player_name = None
                        for direction_word in (" over ", " under "):
                            idx = lower_rn.find(direction_word)
                            if idx != -1:
                                player_name = runner_name[:idx].strip()
                                break
                        if not player_name:
                            continue

                        line = float(handicap)
                        key = (player_name, stat_type)
                        if key not in seen:
                            seen[key] = {
                                "sport": sport_name,
                                "projection_id": str(runner_id),
                                "player_name": player_name,
                                "team_abbr": "",
                                "event_home_abbr": "",
                                "event_away_abbr": "",
                                "position": "",
                                "stat_type": stat_type,
                                "line": line,
                                "start_time": "",
                                "pick_type": "standard",
                                "n_variants": 1,
                                "from_multiple_lines": False,
                                "prop_source": "FanDuel",
                            }
                    except Exception:
                        continue
            except Exception:
                continue

        props = list(seen.values())
        logger.info("FanDuel: %d %s props parsed", len(props), sport_name)
        return props
