#!/usr/bin/env python3
"""
One-time multi-season game-log backfill.

Pulls historical seasons from each sport's API and stores them in the
permanent GameLogArchive so the analyzer has deep history from day one.
The operation is fully idempotent — re-running is a safe no-op (rows already
in the archive are overwritten with the same data).

Usage:
    python scripts/backfill.py [--sport NBA] [--seasons 3] [--players FILE]

Arguments:
    --sport     One of NBA, NHL, MLB, NFL (default: all)
    --seasons   Number of past seasons to backfill (default: 3)
    --players   Optional path to a text file with one player name per line.
                If omitted, the script reads all player IDs from today's
                PrizePicks props (requires a valid API key in config.json).

Example (from repo root):
    python scripts/backfill.py --sport NBA --seasons 2
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date
from pathlib import Path

# Allow running from repo root without installing as a package.
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

from config import load_config, NBA_SEASON_YEAR, SPORT_CONFIG
from data.game_log_archive import GameLogArchive

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger("backfill")

_ARCHIVE = GameLogArchive()


# ---------------------------------------------------------------------------
# NBA backfill
# ---------------------------------------------------------------------------

def _nba_season_str(year: int) -> str:
    return f"{year}-{str(year + 1)[-2:]}"


def backfill_nba(player_ids: list[int], n_seasons: int) -> None:
    from nba_api.stats.endpoints import playergamelog
    from config import NBA_API_TIMEOUT

    current_year = NBA_SEASON_YEAR
    seasons = [_nba_season_str(current_year - i) for i in range(1, n_seasons + 1)]
    logger.info("NBA backfill: %d players × %d seasons = %s", len(player_ids), n_seasons, seasons)

    for player_id in player_ids:
        for season in seasons:
            for season_type in ("Regular Season", "Playoffs"):
                try:
                    ep = playergamelog.PlayerGameLog(
                        player_id=player_id,
                        season=season,
                        season_type_all_star=season_type,
                        timeout=NBA_API_TIMEOUT,
                    )
                    df = ep.get_data_frames()[0]
                    if df.empty:
                        continue
                    df = df.copy()
                    df["location"] = df["MATCHUP"].apply(
                        lambda m: "Home" if "vs." in str(m) else "Away"
                    )
                    df["opponent_abbr"] = df["MATCHUP"].apply(
                        lambda m: str(m).split()[-1] if "@" in str(m) else str(m).split()[-1]
                    )
                    df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"], format="%b %d, %Y", errors="coerce")
                    df = df.dropna(subset=["GAME_DATE"])
                    # Filter numeric cols only for the archive payload.
                    n = _ARCHIVE.merge("NBA", player_id, df)
                    logger.info(
                        "NBA player %d  %s %s → %d rows archived",
                        player_id, season, season_type, n,
                    )
                    time.sleep(0.6)
                except Exception as exc:
                    logger.warning("NBA player %d %s %s failed: %s", player_id, season, season_type, exc)
                    time.sleep(1.0)


# ---------------------------------------------------------------------------
# NHL backfill
# ---------------------------------------------------------------------------

def _nhl_season_str(year: int) -> str:
    return f"{year}{year + 1}"


def backfill_nhl(player_ids: list[int], n_seasons: int) -> None:
    import requests
    from config import STATS_API_TIMEOUT

    current_year = date.today().year
    # If before August (off-season), the current season is previous year.
    if date.today().month < 8:
        current_year -= 1

    seasons = [_nhl_season_str(current_year - i) for i in range(1, n_seasons + 1)]
    logger.info("NHL backfill: %d players × %d seasons = %s", len(player_ids), n_seasons, seasons)

    for player_id in player_ids:
        for season in seasons:
            rows: list[dict] = []
            for game_type in (2, 3):
                try:
                    resp = requests.get(
                        f"https://api-web.nhle.com/v1/player/{player_id}/game-log/{season}/{game_type}",
                        timeout=STATS_API_TIMEOUT,
                    )
                    if resp.status_code == 404:
                        continue
                    resp.raise_for_status()
                    for g in resp.json().get("gameLog", []):
                        gd = g.get("gameDate", "")
                        if not gd:
                            continue
                        opp = g.get("opponentAbbrev", "")
                        home = g.get("homeRoadFlag", "H") == "H"
                        rows.append({
                            "GAME_DATE": gd,
                            "location": "Home" if home else "Away",
                            "opponent_abbr": opp,
                            "G": float(g.get("goals", 0) or 0),
                            "A": float(g.get("assists", 0) or 0),
                            "PTS": float(g.get("points", 0) or 0),
                            "SOG": float(g.get("shots", 0) or 0),
                            "PIM": float(g.get("pim", 0) or 0),
                            "TOI": g.get("toi", "0:00"),
                        })
                    time.sleep(0.3)
                except Exception as exc:
                    logger.warning("NHL player %d %s gt=%d failed: %s", player_id, season, game_type, exc)

            if rows:
                df = pd.DataFrame(rows)
                df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"], errors="coerce")
                df = df.dropna(subset=["GAME_DATE"])
                n = _ARCHIVE.merge("NHL", player_id, df)
                logger.info("NHL player %d  %s → %d rows archived", player_id, season, n)


# ---------------------------------------------------------------------------
# MLB backfill
# ---------------------------------------------------------------------------

def backfill_mlb(player_ids: list[int], n_seasons: int) -> None:
    import requests
    from config import STATS_API_TIMEOUT

    current_year = date.today().year
    years = [current_year - i for i in range(1, n_seasons + 1)]
    logger.info("MLB backfill: %d players × %d seasons = %s", len(player_ids), n_seasons, years)

    for player_id in player_ids:
        for year in years:
            rows_all: list[pd.DataFrame] = []
            for group in ("hitting", "pitching"):
                for game_type in ("R", "P"):
                    try:
                        resp = requests.get(
                            f"https://statsapi.mlb.com/api/v1/people/{player_id}/stats",
                            params={
                                "stats": "gameLog",
                                "season": year,
                                "group": group,
                                "gameType": game_type,
                            },
                            timeout=STATS_API_TIMEOUT,
                        )
                        resp.raise_for_status()
                        splits = resp.json().get("stats", [{}])[0].get("splits", [])
                        sub_rows = []
                        for s in splits:
                            gd = s.get("date", "")
                            st = s.get("stat", {})
                            team = s.get("team", {}).get("abbreviation", "")
                            opp = s.get("opponent", {}).get("abbreviation", "")
                            is_home = s.get("isHome", True)
                            sub_rows.append({
                                "GAME_DATE": gd,
                                "location": "Home" if is_home else "Away",
                                "opponent_abbr": opp,
                                "H": float(st.get("hits", 0) or 0),
                                "HR": float(st.get("homeRuns", 0) or 0),
                                "RBI": float(st.get("rbi", 0) or 0),
                                "SB": float(st.get("stolenBases", 0) or 0),
                                "SO": float(st.get("strikeOuts", 0) or 0),
                                "BB": float(st.get("baseOnBalls", 0) or 0),
                                "R": float(st.get("runs", 0) or 0),
                            })
                        if sub_rows:
                            rows_all.append(pd.DataFrame(sub_rows))
                        time.sleep(0.3)
                    except Exception as exc:
                        logger.warning(
                            "MLB player %d %d %s %s failed: %s",
                            player_id, year, group, game_type, exc,
                        )

            if rows_all:
                df = pd.concat(rows_all, ignore_index=True)
                df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"], errors="coerce")
                df = df.dropna(subset=["GAME_DATE"])
                df = df.drop_duplicates(subset=["GAME_DATE"]).reset_index(drop=True)
                n = _ARCHIVE.merge("MLB", player_id, df)
                logger.info("MLB player %d  %d → %d rows archived", player_id, year, n)


# ---------------------------------------------------------------------------
# NFL backfill
# ---------------------------------------------------------------------------

def backfill_nfl(player_ids: list[int], n_seasons: int) -> None:
    try:
        import nfl_data_py as nfl  # type: ignore[import]
    except ImportError:
        logger.warning("nfl_data_py not installed — skipping NFL backfill")
        return

    current_year = date.today().year
    years = [current_year - i for i in range(1, n_seasons + 1)]
    logger.info("NFL backfill: %d players × %d seasons = %s", len(player_ids), n_seasons, years)

    try:
        weekly = nfl.import_weekly_data(years)
    except Exception as exc:
        logger.warning("NFL weekly data fetch failed: %s", exc)
        return

    schedule_map: dict[tuple, pd.Timestamp] = {}
    try:
        sched = nfl.import_schedules(years)
        for _, row in sched.iterrows():
            yr = int(row.get("season", 0))
            wk = int(row.get("week", 0))
            gd = pd.to_datetime(row.get("gameday", None), errors="coerce")
            if not pd.isna(gd):
                schedule_map[(yr, wk)] = gd
    except Exception:
        pass

    for player_id in player_ids:
        pid_str = str(player_id)
        player_data = weekly[weekly["player_id"].astype(str) == pid_str]
        if player_data.empty:
            continue
        rows = []
        for _, row in player_data.iterrows():
            week = int(row.get("week", 0))
            yr = int(row.get("season", date.today().year))
            game_date = schedule_map.get((yr, week), pd.NaT)
            team = str(row.get("recent_team", ""))
            opp = str(row.get("opponent_team", ""))
            is_home = row.get("home_team", "") == team
            rows.append({
                "GAME_DATE": game_date,
                "location": "Home" if is_home else "Away",
                "opponent_abbr": opp,
                "PASS_YDS": float(row.get("passing_yards", 0) or 0),
                "PASS_TDS": float(row.get("passing_tds", 0) or 0),
                "RUSH_YDS": float(row.get("rushing_yards", 0) or 0),
                "REC": float(row.get("receptions", 0) or 0),
                "REC_YDS": float(row.get("receiving_yards", 0) or 0),
            })
        if rows:
            df = pd.DataFrame(rows)
            df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"], errors="coerce")
            df = df.dropna(subset=["GAME_DATE"])
            n = _ARCHIVE.merge("NFL", player_id, df)
            logger.info("NFL player %d  %s → %d rows archived", player_id, years, n)


# ---------------------------------------------------------------------------
# Player ID discovery
# ---------------------------------------------------------------------------

def _discover_player_ids(sport: str, cfg: dict) -> list[int]:
    """Try to pull today's prop player IDs from PrizePicks."""
    try:
        from data.prizepicks_client import OddsAPIClient
        client = OddsAPIClient()
        sport_cfg = SPORT_CONFIG.get(sport, {})
        props = client.fetch_props(sport_cfg)
        ids = []
        stats_client = _make_stats_client(sport, cfg)
        for p in props:
            pid = stats_client.find_player_id(p.get("player_name", ""))
            if pid:
                ids.append(pid)
        return list(set(ids))
    except Exception as exc:
        logger.warning("Could not auto-discover player IDs for %s: %s", sport, exc)
        return []


def _make_stats_client(sport: str, cfg: dict):
    if sport == "NBA":
        from data.nba_stats_client import NBAStatsClient
        return NBAStatsClient(balldontlie_api_key=cfg.get("balldontlie_api_key"))
    if sport == "NHL":
        from data.nhl_stats_client import NHLStatsClient
        return NHLStatsClient()
    if sport == "MLB":
        from data.mlb_stats_client import MLBStatsClient
        return MLBStatsClient()
    if sport == "NFL":
        from data.nfl_stats_client import NFLStatsClient
        return NFLStatsClient()
    raise ValueError(f"Unknown sport: {sport}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill historical game logs into the archive.")
    parser.add_argument("--sport", choices=["NBA", "NHL", "MLB", "NFL", "all"], default="all")
    parser.add_argument("--seasons", type=int, default=3, help="Number of past seasons to backfill")
    parser.add_argument(
        "--players", type=str, default=None,
        help="Path to text file with one player name per line (optional)"
    )
    args = parser.parse_args()

    try:
        cfg = load_config()
    except Exception as exc:
        logger.warning("Config load failed (%s) — proceeding with defaults", exc)
        cfg = {}

    sports = ["NBA", "NHL", "MLB", "NFL"] if args.sport == "all" else [args.sport]

    for sport in sports:
        logger.info("=" * 50)
        logger.info("Backfilling %s  (%d seasons)", sport, args.seasons)

        if args.players:
            names = [n.strip() for n in Path(args.players).read_text().splitlines() if n.strip()]
            client = _make_stats_client(sport, cfg)
            player_ids = []
            for name in names:
                pid = client.find_player_id(name)
                if pid:
                    player_ids.append(pid)
                    logger.info("  %s → %d", name, pid)
                else:
                    logger.warning("  %s → not found", name)
        else:
            logger.info("Auto-discovering players from today's PrizePicks props…")
            player_ids = _discover_player_ids(sport, cfg)
            if not player_ids:
                logger.warning("No player IDs found for %s — pass --players FILE to backfill manually", sport)
                continue

        logger.info("Backfilling %d players for %s", len(player_ids), sport)
        if sport == "NBA":
            backfill_nba(player_ids, args.seasons)
        elif sport == "NHL":
            backfill_nhl(player_ids, args.seasons)
        elif sport == "MLB":
            backfill_mlb(player_ids, args.seasons)
        elif sport == "NFL":
            backfill_nfl(player_ids, args.seasons)

    logger.info("Backfill complete.")


if __name__ == "__main__":
    main()
