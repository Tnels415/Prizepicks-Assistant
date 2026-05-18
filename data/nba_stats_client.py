from __future__ import annotations

import logging
import time
import json
from datetime import date

import pandas as pd
import requests
from tenacity import (
    retry, stop_after_attempt, wait_exponential,
    retry_if_exception_type, before_sleep_log,
)

from config import (
    NBA_SEASON, NBA_SEASON_TYPE, NBA_SEASON_YEAR, NBA_API_TIMEOUT,
    NBA_API_RETRY_ATTEMPTS, NBA_API_RETRY_MIN_WAIT, NBA_API_RETRY_MAX_WAIT,
    BALLDONTLIE_BASE_URL,
)
from data.game_log_cache import GameLogCache

logger = logging.getLogger(__name__)

# Patch nba_api headers to improve stats.nba.com compatibility
try:
    from nba_api.stats.library import http as _nba_http
    _nba_http.STATS_HEADERS.update({
        "Origin": "https://www.nba.com",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "x-nba-stats-origin": "stats",
        "x-nba-stats-token": "true",
    })
except Exception:
    pass

# Module-level cache singleton shared across all NBAStatsClient instances.
_GAME_LOG_CACHE = GameLogCache()

_PLAYER_CACHE: dict[int, pd.DataFrame] = {}
_TEAM_STATS_CACHE: pd.DataFrame | None = None
_PLAYER_USAGE_CACHE: dict[int, float] = {}

# Set to True after the first BallDontLie 401 so we stop hammering a
# paid-only endpoint for every remaining player in the same run.
_BDL_GAME_LOG_DISABLED: bool = False

# Set to True after stats.nba.com times out so subsequent players skip the
# live call entirely and go straight to disk cache / BDL fallback.
_NBA_API_UNAVAILABLE: bool = False

# Set to True after the ESPN API returns an error so subsequent players skip it.
_ESPN_UNAVAILABLE: bool = False

_ESPN_SEARCH_URL = "https://site.api.espn.com/apis/common/v3/search"
_ESPN_GAMELOG_URL = "https://site.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{}/gamelog"

# Maps ESPN gamelog label strings to our internal DataFrame column names.
_ESPN_LABEL_MAP: dict[str, str] = {
    "PTS":  "PTS",
    "REB":  "REB",
    "AST":  "AST",
    "STL":  "STL",
    "BLK":  "BLK",
    "TO":   "TOV",
    "TOV":  "TOV",
    "3PM":  "FG3M",
    "3FGM": "FG3M",
    "MIN":  "MIN",
    "FGM":  "FGM",
    "FGA":  "FGA",
    "3PA":  "FG3A",
    "3FGA": "FG3A",
    "FTM":  "FTM",
    "FTA":  "FTA",
    "DREB": "DREB",
    "OREB": "OREB",
    "PF":   "PF",
    "+/-":  "PLUS_MINUS",
    "PM":   "PLUS_MINUS",
}


class NBAStatsClient:

    def __init__(self, balldontlie_api_key: str | None = None):
        self._bdl_key = balldontlie_api_key
        # nba_api player_id → original search name, populated by find_player_id.
        # Needed because BallDontLie uses its own numeric ID scheme that differs
        # from nba_api IDs; a name-based search is required to get the correct BDL ID.
        self._id_to_name: dict[int, str] = {}

    # -------------------------------------------------------------------------
    # Player ID lookup
    # -------------------------------------------------------------------------

    def find_player_id(self, player_name: str) -> int | None:
        from nba_api.stats.static import players as nba_players
        matches = nba_players.find_players_by_full_name(player_name)
        if matches:
            active = [p for p in matches if p.get("is_active")]
            best = active[0] if active else matches[0]
            pid = best["id"]
            self._id_to_name[pid] = player_name
            return pid

        # Fuzzy fallback: try matching by last name
        name_parts = player_name.strip().split()
        if len(name_parts) >= 2:
            last = name_parts[-1]
            candidates = nba_players.find_players_by_last_name(last)
            if candidates:
                active = [p for p in candidates if p.get("is_active")]
                best = active[0] if active else candidates[0]
                pid = best["id"]
                self._id_to_name[pid] = player_name
                return pid

        # balldontlie fallback
        if self._bdl_key:
            return self._bdl_find_player(player_name)

        logger.warning("Could not find player ID for: %s", player_name)
        return None

    def _bdl_find_player(self, name: str) -> int | None:
        try:
            resp = self._bdl_get("/players", {"search": name, "per_page": 5})
            results = resp.get("data", [])
            if results:
                return results[0]["id"]
        except Exception as exc:
            logger.debug("balldontlie player search failed for %s: %s", name, exc)
        return None

    # -------------------------------------------------------------------------
    # Player game log (current season, all games)
    # -------------------------------------------------------------------------

    def get_player_game_log(self, player_id: int) -> pd.DataFrame:
        if player_id in _PLAYER_CACHE:
            return _PLAYER_CACHE[player_id]

        # 1. Check disk cache first (valid for 24 hours).
        cached = _GAME_LOG_CACHE.get("NBA", player_id)
        if cached is not None:
            logger.debug("NBA game log for player %d served from disk cache", player_id)
            _PLAYER_CACHE[player_id] = cached
            return cached

        # 2. Try live nba_api (skip if stats.nba.com already timed out this run).
        df = None if _NBA_API_UNAVAILABLE else self._fetch_game_log_nba_api(player_id)

        # 3. ESPN unofficial API — free, no auth, reliable game logs.
        if (df is None or df.empty) and not _ESPN_UNAVAILABLE:
            player_name = self._id_to_name.get(player_id, "")
            if player_name:
                df = self._fetch_game_log_espn(player_name)

        if df is None or df.empty:
            # Look up the original search name so BallDontLie can find its own ID.
            player_name = self._id_to_name.get(player_id, "")
            df = self._fetch_game_log_bdl(player_id, player_name)

        # 3. On success, persist to disk cache.
        if df is not None and not df.empty:
            _GAME_LOG_CACHE.set("NBA", player_id, df)
        else:
            # 4. Both live sources failed — try stale cache (< 7 days old).
            stale = _GAME_LOG_CACHE.get_stale("NBA", player_id)
            if stale is not None:
                df = stale
            else:
                df = pd.DataFrame()

        _PLAYER_CACHE[player_id] = df
        return df

    def _fetch_game_log_nba_api(self, player_id: int) -> pd.DataFrame | None:
        # Fetch both Regular Season and Playoffs so the app works correctly
        # regardless of the current phase (regular season, play-in, or playoffs).
        from nba_api.stats.endpoints import playergamelog
        all_dfs = []
        for season_type in ("Regular Season", "Playoffs"):
            try:
                endpoint = self._nba_api_call(
                    playergamelog.PlayerGameLog,
                    player_id=player_id,
                    season=NBA_SEASON,
                    season_type_all_star=season_type,
                    timeout=NBA_API_TIMEOUT,
                )
                if endpoint is None:
                    continue
                df = endpoint.get_data_frames()[0]
                if df.empty:
                    continue
                df = df.copy()
                df["location"] = df["MATCHUP"].apply(
                    lambda m: "Home" if "vs." in str(m) else "Away"
                )
                df["opponent_abbr"] = df["MATCHUP"].apply(self._parse_opponent_abbr)
                df["GAME_DATE"] = pd.to_datetime(
                    df["GAME_DATE"], format="%b %d, %Y", errors="coerce"
                )
                all_dfs.append(df)
            except Exception as exc:
                logger.debug(
                    "nba_api game log (%s) failed for player %d: %s",
                    season_type, player_id, exc,
                )

        if not all_dfs:
            return None
        combined = pd.concat(all_dfs, ignore_index=True)
        combined = combined.sort_values("GAME_DATE", ascending=False).reset_index(drop=True)
        return combined

    def _fetch_game_log_bdl(self, player_id: int, player_name: str = "") -> pd.DataFrame | None:
        global _BDL_GAME_LOG_DISABLED
        if not self._bdl_key or _BDL_GAME_LOG_DISABLED:
            return None

        # BallDontLie uses its own player ID scheme that is completely different
        # from nba_api (stats.nba.com) IDs.  We must search BDL by name to get
        # the correct BDL player ID before querying the stats endpoint.
        if not player_name:
            logger.debug(
                "BallDontLie fallback skipped for player %d — name unknown", player_id
            )
            return None

        bdl_player_id = self._bdl_find_player(player_name)
        if bdl_player_id is None:
            logger.debug("BallDontLie: '%s' not found — skipping BDL game log", player_name)
            return None

        try:
            from config import NBA_SEASON_YEAR
            records = self._bdl_paginate(
                "/stats",
                {"player_ids[]": bdl_player_id, "seasons[]": NBA_SEASON_YEAR, "per_page": 100},
            )
            if not records:
                return None
            rows = []
            for r in records:
                game = r.get("game", {})
                home_id = game.get("home_team_id")
                team_id = r.get("team", {}).get("id")
                location = "Home" if team_id == home_id else "Away"
                matchup = r.get("team", {}).get("abbreviation", "")
                rows.append({
                    "GAME_DATE":    pd.to_datetime(r.get("date", "")),
                    "MATCHUP":      matchup,
                    "location":     location,
                    "opponent_abbr": "",
                    "MIN":          r.get("min", "0"),
                    "PTS":          r.get("pts", 0),
                    "REB":          r.get("reb", 0),
                    "AST":          r.get("ast", 0),
                    "FGM":          r.get("fgm", 0),
                    "FGA":          r.get("fga", 0),
                    "FG3M":         r.get("fg3m", 0),
                    "FG3A":         r.get("fg3a", 0),
                    "FTM":          r.get("ftm", 0),
                    "FTA":          r.get("fta", 0),
                    "OREB":         r.get("oreb", 0),
                    "DREB":         r.get("dreb", 0),
                    "STL":          r.get("stl", 0),
                    "BLK":          r.get("blk", 0),
                    "TOV":          r.get("turnover", 0),
                    "PF":           r.get("pf", 0),
                })
            df = pd.DataFrame(rows)
            df = df.sort_values("GAME_DATE", ascending=False).reset_index(drop=True)
            return df
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 401:
                # Disable BDL game-log for the rest of this run and log once at
                # WARNING so the user knows why it stopped working.
                _BDL_GAME_LOG_DISABLED = True
                logger.warning(
                    "BallDontLie stats endpoint requires a paid plan (HTTP 401). "
                    "BDL game-log fallback disabled for this run — nba_api is the primary source. "
                    "To suppress this warning, remove BALLDONTLIE_API_KEY from .env."
                )
            else:
                logger.debug("BallDontLie game log failed for '%s': %s", player_name, exc)
            return None

    def _find_espn_id(self, player_name: str) -> str | None:
        try:
            resp = requests.get(
                _ESPN_SEARCH_URL,
                params={
                    "query": player_name,
                    "limit": 5,
                    "type": "athlete",
                    "sport": "basketball",
                    "league": "nba",
                },
                timeout=10,
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
            if not items:
                logger.debug("ESPN search: no results for '%s'", player_name)
                return None
            uid = items[0].get("uid", "")
            if "~a:" not in uid:
                return None
            return uid.split("~a:")[-1]
        except Exception as exc:
            logger.debug("ESPN player search failed for '%s': %s", player_name, exc)
            return None

    def _fetch_game_log_espn(self, player_name: str) -> pd.DataFrame | None:
        global _ESPN_UNAVAILABLE
        espn_id = self._find_espn_id(player_name)
        if espn_id is None:
            return None
        try:
            url = _ESPN_GAMELOG_URL.format(espn_id)
            resp = requests.get(
                url,
                params={"season": NBA_SEASON_YEAR},
                timeout=10,
            )
            resp.raise_for_status()
            df = self._parse_espn_gamelog(resp.json())
            if df is not None and not df.empty:
                logger.debug("ESPN: fetched %d games for '%s'", len(df), player_name)
            return df
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 404:
                logger.debug("ESPN gamelog 404 for '%s' (ESPN id=%s)", player_name, espn_id)
            else:
                logger.warning("ESPN gamelog HTTP %s for '%s': %s", status, player_name, exc)
                _ESPN_UNAVAILABLE = True
            return None
        except Exception as exc:
            logger.debug("ESPN gamelog fetch failed for '%s': %s", player_name, exc)
            return None

    def _parse_espn_gamelog(self, data: dict) -> pd.DataFrame | None:
        labels: list[str] = data.get("labels", [])
        events: dict = data.get("events", {})
        if not labels or not events:
            return None

        # Build a column-index map from label → position in stats array.
        col_idx: dict[str, int] = {}
        for i, lbl in enumerate(labels):
            mapped = _ESPN_LABEL_MAP.get(lbl)
            if mapped and mapped not in col_idx:
                col_idx[mapped] = i

        rows = []
        for _event_id, event in events.items():
            stats_arr = event.get("statistics", [])
            if not stats_arr:
                continue

            home_away = event.get("homeAway", "").lower()
            location = "Home" if home_away == "home" else "Away"
            opp = (event.get("opponent") or {}).get("abbreviation", "")
            raw_date = event.get("date", "")
            try:
                game_date = pd.to_datetime(raw_date, utc=True).tz_convert(None)
            except Exception:
                game_date = pd.NaT

            def _stat(col: str) -> float:
                idx = col_idx.get(col)
                if idx is None or idx >= len(stats_arr):
                    return 0.0
                try:
                    return float(stats_arr[idx])
                except (TypeError, ValueError):
                    return 0.0

            rows.append({
                "GAME_DATE":     game_date,
                "MATCHUP":       opp,
                "location":      location,
                "opponent_abbr": opp,
                "MIN":           str(int(_stat("MIN"))) if _stat("MIN") else "0",
                "PTS":           _stat("PTS"),
                "REB":           _stat("REB"),
                "AST":           _stat("AST"),
                "FGM":           _stat("FGM"),
                "FGA":           _stat("FGA"),
                "FG3M":          _stat("FG3M"),
                "FG3A":          _stat("FG3A"),
                "FTM":           _stat("FTM"),
                "FTA":           _stat("FTA"),
                "OREB":          _stat("OREB"),
                "DREB":          _stat("DREB"),
                "STL":           _stat("STL"),
                "BLK":           _stat("BLK"),
                "TOV":           _stat("TOV"),
                "PF":            _stat("PF"),
                "PLUS_MINUS":    _stat("PLUS_MINUS"),
            })

        if not rows:
            return None
        df = pd.DataFrame(rows)
        df = df.sort_values("GAME_DATE", ascending=False).reset_index(drop=True)
        return df

    @staticmethod
    def _parse_opponent_abbr(matchup: str) -> str:
        parts = str(matchup).split()
        if len(parts) >= 3:
            return parts[-1]
        return ""

    # -------------------------------------------------------------------------
    # Player home/away and rest-day splits
    # -------------------------------------------------------------------------

    def get_player_splits(self, player_id: int) -> dict:
        try:
            from nba_api.stats.endpoints import playerdashboardbygeneralsplits
            endpoint = self._nba_api_call(
                playerdashboardbygeneralsplits.PlayerDashboardByGeneralSplits,
                player_id=player_id,
                season=NBA_SEASON,
                season_type_playoffs="Regular Season",
                measure_type_detailed_defense="Base",
                per_mode_detailed="PerGame",
                timeout=NBA_API_TIMEOUT,
            )
            if endpoint is None:
                return {}

            result = {}
            dfs = endpoint.get_data_frames()
            # Index varies by nba_api version; find by dataset name
            dataset_names = [d.name for d in endpoint.get_normalized_dict().get("resultSets", [])]

            location_df = None
            rest_df = None
            for i, df in enumerate(dfs):
                name = dataset_names[i] if i < len(dataset_names) else ""
                if "Location" in name and not df.empty:
                    location_df = df
                if "DaysRest" in name and not df.empty:
                    rest_df = df

            if location_df is not None:
                for _, row in location_df.iterrows():
                    gv = str(row.get("GROUP_VALUE", "")).lower()
                    if gv in ("home", "road"):
                        key = "home" if gv == "home" else "away"
                        result[key] = row.to_dict()

            if rest_df is not None:
                for _, row in rest_df.iterrows():
                    gv = str(row.get("GROUP_VALUE", ""))
                    if gv == "0":
                        result["rest_0"] = row.to_dict()
                    elif gv == "1":
                        result["rest_1"] = row.to_dict()
                    elif gv in ("2", "3", "4+", "5+"):
                        result["rest_2plus"] = row.to_dict()

            return result
        except Exception as exc:
            logger.debug("Player splits failed for player %d: %s", player_id, exc)
            return {}

    # -------------------------------------------------------------------------
    # Team advanced + opponent defensive stats (cached, called once)
    # -------------------------------------------------------------------------

    def get_team_advanced_stats(self) -> pd.DataFrame:
        global _TEAM_STATS_CACHE
        if _TEAM_STATS_CACHE is not None:
            return _TEAM_STATS_CACHE

        adv_df = self._fetch_team_stats(measure_type="Advanced")
        opp_df = self._fetch_team_stats(measure_type="Opponent")

        if adv_df is None and opp_df is None:
            _TEAM_STATS_CACHE = pd.DataFrame()
            return _TEAM_STATS_CACHE

        if adv_df is not None and opp_df is not None:
            merged = adv_df.merge(opp_df, on="TEAM_ID", suffixes=("", "_OPP"))
        elif adv_df is not None:
            merged = adv_df
        else:
            merged = opp_df

        _TEAM_STATS_CACHE = merged
        return _TEAM_STATS_CACHE

    def _fetch_team_stats(self, measure_type: str) -> pd.DataFrame | None:
        try:
            from nba_api.stats.endpoints import leaguedashteamstats
            endpoint = self._nba_api_call(
                leaguedashteamstats.LeagueDashTeamStats,
                season=NBA_SEASON,
                season_type_all_star=NBA_SEASON_TYPE,
                measure_type_detailed_defense=measure_type,
                per_mode_detailed="PerGame",
                timeout=NBA_API_TIMEOUT,
            )
            if endpoint is None:
                return None
            df = endpoint.get_data_frames()[0]
            return df if not df.empty else None
        except Exception as exc:
            logger.warning("Team stats (%s) fetch failed: %s", measure_type, exc)
            return None

    # -------------------------------------------------------------------------
    # Player usage rate (cached, called once)
    # -------------------------------------------------------------------------

    def get_player_usage(self) -> dict[int, float]:
        global _PLAYER_USAGE_CACHE
        if _PLAYER_USAGE_CACHE:
            return _PLAYER_USAGE_CACHE

        try:
            from nba_api.stats.endpoints import leaguedashplayerstats
            endpoint = self._nba_api_call(
                leaguedashplayerstats.LeagueDashPlayerStats,
                season=NBA_SEASON,
                season_type_all_star=NBA_SEASON_TYPE,
                measure_type_detailed_defense="Advanced",
                per_mode_detailed="PerGame",
                timeout=NBA_API_TIMEOUT,
            )
            if endpoint is None:
                return {}
            df = endpoint.get_data_frames()[0]
            if "PLAYER_ID" in df.columns and "USG_PCT" in df.columns:
                _PLAYER_USAGE_CACHE = dict(zip(df["PLAYER_ID"].astype(int), df["USG_PCT"]))
        except Exception as exc:
            logger.warning("Player usage fetch failed: %s", exc)

        return _PLAYER_USAGE_CACHE

    # -------------------------------------------------------------------------
    # Outcome evaluation helper
    # -------------------------------------------------------------------------

    def get_game_stats_for_date(self, player_id: int, game_date: date) -> dict | None:
        # Bypass disk cache so outcome evaluation always uses completed-game data.
        # The cache was populated before yesterday's games were played; without
        # invalidation the fetcher would see stale data and return {"played": False}.
        _GAME_LOG_CACHE.invalidate("NBA", player_id)
        if player_id in _PLAYER_CACHE:
            del _PLAYER_CACHE[player_id]
        return super().get_game_stats_for_date(player_id, game_date)

    # -------------------------------------------------------------------------
    # nba_api retry wrapper
    # -------------------------------------------------------------------------

    def _nba_api_call(self, endpoint_class, **kwargs):
        global _NBA_API_UNAVAILABLE
        last_exc = None
        for attempt in range(NBA_API_RETRY_ATTEMPTS):
            try:
                return endpoint_class(**kwargs)
            except Exception as exc:
                last_exc = exc
                exc_str = str(exc).lower()
                is_timeout = "timed out" in exc_str or "timeout" in exc_str
                if is_timeout:
                    # stats.nba.com is hanging — mark it down for this run so
                    # all remaining players skip the live call immediately.
                    _NBA_API_UNAVAILABLE = True
                    logger.warning(
                        "stats.nba.com timed out — marking NBA stats API unavailable "
                        "for this run; remaining players will use disk cache.",
                    )
                    break  # no point retrying a hanging server
                wait = NBA_API_RETRY_MIN_WAIT * (2 ** attempt)
                logger.debug(
                    "nba_api call %s attempt %d failed: %s — retrying in %ds",
                    endpoint_class.__name__, attempt + 1, exc, wait,
                )
                time.sleep(min(wait, NBA_API_RETRY_MAX_WAIT))
        logger.warning(
            "nba_api call %s failed after %d attempts: %s",
            endpoint_class.__name__, NBA_API_RETRY_ATTEMPTS, last_exc,
        )
        return None

    # -------------------------------------------------------------------------
    # balldontlie helpers
    # -------------------------------------------------------------------------

    def _bdl_get(self, path: str, params: dict) -> dict:
        headers = {"Authorization": f"Bearer {self._bdl_key}"}
        resp = requests.get(
            BALLDONTLIE_BASE_URL + path,
            headers=headers,
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def _bdl_paginate(self, path: str, params: dict) -> list[dict]:
        all_data = []
        cursor = None
        while True:
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            data = self._bdl_get(path, p)
            all_data.extend(data.get("data", []))
            cursor = data.get("meta", {}).get("next_cursor")
            if not cursor:
                break
            time.sleep(1.0)
        return all_data
