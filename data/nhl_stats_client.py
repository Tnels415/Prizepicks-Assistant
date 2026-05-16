from __future__ import annotations

import json
import logging
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

from config import get_nhl_season, STATS_API_TIMEOUT, NHL_GOALIE_FANTASY_FORMULA
from data.base_stats_client import BaseStatsClient
from data.game_log_cache import GameLogCache

logger = logging.getLogger(__name__)

# Module-level cache singleton shared across all NHLStatsClient instances.
_GAME_LOG_CACHE = GameLogCache()

# Set to True after the NHL API times out so remaining players skip live calls.
_NHL_API_UNAVAILABLE: bool = False

NHL_WEB_API = "https://api-web.nhle.com/v1"

# All 32 current NHL franchises — used to build the player DB from rosters.
_NHL_TEAMS = [
    "ANA", "BOS", "BUF", "CGY", "CAR", "CHI", "COL", "CBJ",
    "DAL", "DET", "EDM", "FLA", "LAK", "MIN", "MTL", "NSH",
    "NJD", "NYI", "NYR", "OTT", "PHI", "PIT", "SJS", "SEA",
    "STL", "TBL", "TOR", "UTA", "VAN", "VGK", "WSH", "WPG",
]

# Module-level player cache: lower-case full name → player_id.
# Built once per process from team rosters; avoids the broken search endpoint.
_NHL_PLAYER_DB: dict[str, int] = {}
_NHL_DB_LOADED: bool = False

# Per-game faceoff counts sourced from boxscores.  Keyed by "{game_id}_{player_id}".
# Past game results never change so there is no TTL — data is written once and
# reused forever.  Stored as [fow, foa] lists for JSON round-trip compatibility.
_FACEOFF_CACHE_PATH = Path("cache/nhl_faceoffs.json")
_FACEOFF_CACHE: dict[str, list[float]] | None = None


def _load_faceoff_cache() -> dict[str, list[float]]:
    global _FACEOFF_CACHE
    if _FACEOFF_CACHE is not None:
        return _FACEOFF_CACHE
    if _FACEOFF_CACHE_PATH.exists():
        try:
            _FACEOFF_CACHE = json.loads(_FACEOFF_CACHE_PATH.read_text())
        except Exception:
            _FACEOFF_CACHE = {}
    else:
        _FACEOFF_CACHE = {}
    return _FACEOFF_CACHE


def _save_faceoff_cache() -> None:
    try:
        _FACEOFF_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FACEOFF_CACHE_PATH.write_text(json.dumps(_FACEOFF_CACHE))
    except Exception as exc:
        logger.debug("Failed to persist faceoff cache: %s", exc)


def _compute_goalie_scores(df: pd.DataFrame) -> pd.Series:
    """Compute PrizePicks Goalie Fantasy Score for each game row.

    Formula weights come from config.NHL_GOALIE_FANTASY_FORMULA.
    For skaters DECISION == '' so the result is 0 — only goalie rows
    produce non-zero scores.

    NOTE: The exact PrizePicks formula is not publicly documented.  The
    weights in config are a community approximation.  Verify inside the
    PrizePicks app (tap the scoring-chart icon on any Goalie Fantasy Score
    prop) and update config.NHL_GOALIE_FANTASY_FORMULA accordingly.
    """
    f = NHL_GOALIE_FANTASY_FORMULA
    saves    = df["SAVES"].fillna(0)
    ga       = df["GA"].fillna(0)
    decision = df["DECISION"].fillna("")

    win_pts     = decision.map({"W": f["win"], "OT": f["ot_loss"]}).fillna(0.0)
    # Shutout only when the goalie played a full game (has a decision) and let 0 in
    shutout_pts = (
        (ga == 0) & decision.isin(["W", "L", "OT"])
    ).astype(float) * f["shutout_bonus"]

    return (
        saves * f["save"]
        + win_pts
        + shutout_pts
        + ga * f["goal_against"]
    )


def _name_str(v) -> str:
    """Return a plain string from a name field that may be a str or a multilingual dict."""
    if not v:
        return ""
    return v.get("default", "") if isinstance(v, dict) else str(v)


def _toi_to_minutes(toi_str) -> float:
    """Convert NHL API 'MM:SS' time-on-ice string to decimal minutes."""
    try:
        parts = str(toi_str).split(":")
        if len(parts) == 2:
            return int(parts[0]) + int(parts[1]) / 60.0
        if len(parts) == 3:
            return int(parts[0]) * 60 + int(parts[1]) + int(parts[2]) / 60.0
        return float(toi_str)
    except (ValueError, AttributeError):
        return 0.0


def _build_nhl_player_db() -> None:
    """Populate _NHL_PLAYER_DB from every team's current roster."""
    global _NHL_PLAYER_DB, _NHL_DB_LOADED
    _NHL_DB_LOADED = True  # set early so we don't retry on partial failure
    db: dict[str, int] = {}
    loaded = 0
    for abbrev in _NHL_TEAMS:
        try:
            resp = requests.get(
                f"{NHL_WEB_API}/roster/{abbrev}/current",
                timeout=10,
            )
            if resp.status_code != 200:
                logger.debug("NHL roster %s returned %d", abbrev, resp.status_code)
                continue
            data = resp.json()
            for group in ("forwards", "defensemen", "goalies"):
                for player in data.get(group, []):
                    try:
                        pid = player.get("id")
                        first = _name_str(player.get("firstName"))
                        last = _name_str(player.get("lastName"))
                        if pid and (first or last):
                            db[f"{first} {last}".strip().lower()] = pid
                    except Exception as exc:
                        logger.debug("NHL roster: skipping player entry %s: %s", player, exc)
            loaded += 1
            time.sleep(0.05)
        except Exception as exc:
            logger.debug("NHL roster fetch failed for %s: %s", abbrev, exc)
    _NHL_PLAYER_DB = db
    logger.info(
        "NHL player DB built from %d/%d team rosters — %d players indexed",
        loaded, len(_NHL_TEAMS), len(db),
    )


class NHLStatsClient(BaseStatsClient):

    def __init__(self) -> None:
        self._player_id_cache: dict[str, int | None] = {}
        self._game_log_cache: dict[int, pd.DataFrame] = {}

    # -------------------------------------------------------------------------
    # Player ID lookup
    # -------------------------------------------------------------------------

    def find_player_id(self, player_name: str) -> int | None:
        if player_name in self._player_id_cache:
            return self._player_id_cache[player_name]

        player_id = self._lookup_player(player_name)
        self._player_id_cache[player_name] = player_id
        return player_id

    def _lookup_player(self, player_name: str) -> int | None:
        if not _NHL_DB_LOADED:
            _build_nhl_player_db()

        if not _NHL_PLAYER_DB:
            logger.warning("NHL player DB is empty — cannot find '%s'", player_name)
            return None

        name_lower = player_name.strip().lower()

        # 1. Exact full-name match
        if name_lower in _NHL_PLAYER_DB:
            return _NHL_PLAYER_DB[name_lower]

        # 2. First-name + last-name partial match (handles middle initials, etc.)
        parts = name_lower.split()
        if len(parts) >= 2:
            first, last = parts[0], parts[-1]
            candidates = {
                k: v for k, v in _NHL_PLAYER_DB.items()
                if k.endswith(last) and k.startswith(first)
            }
            if len(candidates) == 1:
                return next(iter(candidates.values()))
            if len(candidates) > 1:
                # Prefer the entry whose name length is closest to the query
                return min(candidates.items(), key=lambda kv: abs(len(kv[0]) - len(name_lower)))[1]

            # 3. Last-name-only fallback (risky but better than nothing)
            last_only = {k: v for k, v in _NHL_PLAYER_DB.items() if k.endswith(f" {last}")}
            if len(last_only) == 1:
                return next(iter(last_only.values()))

        logger.warning("NHL: player '%s' not found in roster DB", player_name)
        return None

    # -------------------------------------------------------------------------
    # Game log
    # -------------------------------------------------------------------------

    def get_player_game_log(self, player_id: int) -> pd.DataFrame:
        if player_id in self._game_log_cache:
            return self._game_log_cache[player_id]

        # 1. Check disk cache first (valid for 24 hours).
        cached = _GAME_LOG_CACHE.get("NHL", player_id)
        if cached is not None:
            logger.debug("NHL game log for player %d served from disk cache", player_id)
            self._game_log_cache[player_id] = cached
            return cached

        # 2. Try live NHL API (skip if already timed out this run).
        season = get_nhl_season()
        rows: list[dict] = []

        if not _NHL_API_UNAVAILABLE:
            for game_type in (2, 3):   # 2 = regular season, 3 = playoffs
                rows.extend(self._fetch_game_log(player_id, season, game_type))
                time.sleep(0.3)

        if not rows:
            df = pd.DataFrame()
        else:
            df = pd.DataFrame(rows)
            df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"])
            df = df.sort_values("GAME_DATE", ascending=False).reset_index(drop=True)
            # Compute derived columns before caching so they survive the CSV round-trip.
            df["GOALIE_SCORE"] = _compute_goalie_scores(df)
            df = self._enrich_game_log_faceoffs(df, player_id)

        # 3. On success, persist to disk cache.
        if not df.empty:
            _GAME_LOG_CACHE.set("NHL", player_id, df)
        else:
            # 4. Live fetch returned nothing — try stale cache (< 7 days old).
            stale = _GAME_LOG_CACHE.get_stale("NHL", player_id)
            if stale is not None:
                df = stale

        self._game_log_cache[player_id] = df
        return df

    def _fetch_game_log(
        self,
        player_id: int,
        season: str,
        game_type: int,
    ) -> list[dict]:
        try:
            resp = requests.get(
                f"{NHL_WEB_API}/player/{player_id}/game-log/{season}/{game_type}",
                timeout=STATS_API_TIMEOUT,
            )
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            exc_str = str(exc).lower()
            if "timed out" in exc_str or "timeout" in exc_str:
                global _NHL_API_UNAVAILABLE
                _NHL_API_UNAVAILABLE = True
                logger.warning(
                    "NHL stats API timed out — marking unavailable for this run; "
                    "remaining players will use disk cache."
                )
            else:
                logger.debug("NHL game log (type=%d) failed for player %d: %s", game_type, player_id, exc)
            return []

        rows = []
        _sample_logged = False
        for g in data.get("gameLog", []):
            # Log all fields from the first game entry so we can identify any
            # faceoff count fields or goalie decision fields in the raw API response.
            if not _sample_logged:
                logger.debug(
                    "NHL game log raw fields (player %d, type=%d): %s",
                    player_id, game_type, sorted(g.keys()),
                )
                logger.debug(
                    "NHL game log sample entry: %s",
                    {k: v for k, v in g.items()
                     if any(x in k.lower() for x in
                            ("face", "fow", "foa", "decision", "shutout", "save",
                             "goal", "shot", "pctg", "win", "loss"))},
                )
                _sample_logged = True

            team_abbr = g.get("teamAbbrev", "")
            opp_abbr = g.get("opponentAbbrev", "")
            is_home = g.get("homeRoadFlag", "R") == "H"
            vs_str = "vs." if is_home else "@"

            # Faceoffs: game log only provides faceoffWinningPctg (a 0.0-1.0 float).
            # Try both the percentage field and any raw-count fields the API may add.
            fo_pctg = float(g.get("faceoffWinningPctg") or 0.0)
            fow_raw = g.get("faceoffsWon") or g.get("faceoffWins") or g.get("fow")
            foa_raw = g.get("faceoffsTaken") or g.get("faceoffsAttempted") or g.get("foa")

            # Goalie-specific fields (absent/null for skaters)
            decision = str(g.get("decision") or "").upper().strip()  # "W", "L", "OT", or ""

            rows.append({
                "GAME_DATE": g.get("gameDate", ""),
                "GAME_ID":   str(g.get("gameId", "")),
                "MATCHUP": f"{team_abbr} {vs_str} {opp_abbr}",
                "location": "Home" if is_home else "Away",
                "opponent_abbr": opp_abbr,
                # Skater stats
                "G":         float(g.get("goals", 0)),
                "A":         float(g.get("assists", 0)),
                "PTS":       float(g.get("points", 0)),
                "SOG":       float(g.get("shots", 0)),
                "PPP":       float(g.get("powerPlayPoints", 0)),
                "HITS":      float(g.get("hits", 0)),
                "BLKS":      float(g.get("blockedShots", 0)),
                "PLUSMINUS": float(g.get("plusMinus", 0)),
                "TOI":       _toi_to_minutes(g.get("toi", "0:00")),
                # Faceoff data: raw counts when available, else 0 (populated later
                # via boxscore enrichment or estimation)
                "FO_PCTG":   fo_pctg,
                "FOW":       float(fow_raw) if fow_raw is not None else 0.0,
                "FOA":       float(foa_raw) if foa_raw is not None else 0.0,
                # Goalie stats (zero for skaters; populated for goalies)
                "SAVES":     float(g.get("saves", 0)),
                "GA":        float(g.get("goalsAgainst", 0)),
                "DECISION":  decision,   # "W", "L", "OT", or "" for skaters
            })
        return rows

    # -------------------------------------------------------------------------
    # Faceoff enrichment — boxscore lookups for per-game FOW/FOA counts
    # -------------------------------------------------------------------------

    def _enrich_game_log_faceoffs(self, df: pd.DataFrame, player_id: int) -> pd.DataFrame:
        """Fill FOW/FOA columns from boxscore data for games where FO_PCTG > 0
        but we only have the percentage (game log doesn't include raw counts).
        Results are persisted in cache/nhl_faceoffs.json — past games are cached
        indefinitely since their stats never change.
        """
        if not {"FOW", "FO_PCTG", "GAME_ID"}.issubset(df.columns):
            return df

        needs = df[(df["FO_PCTG"] > 0) & (df["FOW"] == 0)]
        if needs.empty:
            return df

        cache = _load_faceoff_cache()
        cache_updated = False
        fetched = 0

        for _, row in needs.iterrows():
            game_id = str(row["GAME_ID"]).strip()
            if not game_id:
                continue
            cache_key = f"{game_id}_{player_id}"
            if cache_key in cache:
                fow, foa = cache[cache_key]
            else:
                if fetched >= 15:   # cap live calls to avoid excess API load
                    break
                result = self._fetch_faceoffs_from_boxscore(game_id, player_id)
                fetched += 1
                time.sleep(0.2)
                if result is None:
                    continue
                fow, foa = result
                cache[cache_key] = [fow, foa]
                cache_updated = True

            mask = df["GAME_ID"] == game_id
            df.loc[mask, "FOW"] = fow
            df.loc[mask, "FOA"] = foa

        if cache_updated:
            _save_faceoff_cache()
        if fetched:
            logger.info(
                "NHL faceoff enrichment: %d boxscore(s) fetched for player %d",
                fetched, player_id,
            )
        return df

    def _fetch_faceoffs_from_boxscore(
        self, game_id: str, player_id: int
    ) -> tuple[float, float] | None:
        """Return (faceoffs_won, faceoffs_taken) for player in a specific game."""
        url = f"{NHL_WEB_API}/gamecenter/{game_id}/boxscore"
        try:
            resp = requests.get(url, timeout=STATS_API_TIMEOUT)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.debug("NHL boxscore fetch failed game %s: %s", game_id, exc)
            return None

        for team_key in ("homeTeam", "awayTeam"):
            team = data.get("playerByGameStats", {}).get(team_key, {})
            for group in ("forwards", "defense", "defensemen"):
                for player in team.get(group, []):
                    if player.get("playerId") == player_id:
                        fow = float(
                            player.get("faceoffWins")
                            or player.get("faceoffsWon")
                            or 0
                        )
                        foa = float(
                            player.get("faceoffsTaken")
                            or player.get("faceoffsAttempted")
                            or 0
                        )
                        logger.debug(
                            "Boxscore game %s player %d: FOW=%.0f FOA=%.0f",
                            game_id, player_id, fow, foa,
                        )
                        return fow, foa

        logger.debug(
            "NHL boxscore game %s: player %d not found in skater lists", game_id, player_id
        )
        return None

    # -------------------------------------------------------------------------
    # Team stats (not available → return empty; factors degrade gracefully)
    # -------------------------------------------------------------------------

    def get_team_advanced_stats(self) -> pd.DataFrame:
        return pd.DataFrame()

    # -------------------------------------------------------------------------
    # Outcome evaluation helper
    # -------------------------------------------------------------------------

    def get_game_stats_for_date(self, player_id: int, game_date: date) -> dict | None:
        try:
            df = self.get_player_game_log(player_id)
            if df.empty or "GAME_DATE" not in df.columns:
                return {"played": False}
            mask = df["GAME_DATE"].dt.date == game_date
            rows = df[mask]
            if rows.empty:
                return {"played": False}
            row = rows.iloc[0]
            stats = {
                col: float(row[col])
                for col in ("G", "A", "PTS", "SOG", "PPP", "HITS",
                            "BLKS", "PLUSMINUS", "TOI", "SAVES", "GA",
                            "FOW", "GOALIE_SCORE")
                if col in row.index
            }
            stats["played"] = True
            return stats
        except Exception as exc:
            logger.warning("NHL get_game_stats_for_date failed (player %d): %s", player_id, exc)
            return None
