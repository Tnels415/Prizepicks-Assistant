from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from data.base_stats_client import BaseStatsClient
from data.game_log_archive import GameLogArchive

_ARCHIVE = GameLogArchive()


def _merge_with_archive(player_id: int, fresh: pd.DataFrame) -> pd.DataFrame:
    """Return union of *fresh* and the permanent archive, deduped newest-first."""
    archived = _ARCHIVE.get("NFL", player_id)
    if archived is None or archived.empty:
        return fresh if fresh is not None else pd.DataFrame()
    if fresh is None or fresh.empty:
        return archived
    combined = pd.concat([fresh, archived], ignore_index=True)
    if "GAME_DATE" in combined.columns:
        combined = (
            combined
            .drop_duplicates(subset=["GAME_DATE"])
            .sort_values("GAME_DATE", ascending=False)
            .reset_index(drop=True)
        )
    return combined

logger = logging.getLogger(__name__)


class NFLStatsClient(BaseStatsClient):
    """
    NFL stats via nfl_data_py (https://github.com/nflverse/nfl_data_py).

    This library is required only when the NFL is in season (Sep–Jan).
    Import is deferred so the rest of the system works without it installed.
    """

    def __init__(self) -> None:
        self._player_id_cache: dict[str, str | None] = {}
        self._weekly_cache: dict[int, pd.DataFrame] = {}
        self._roster_cache: pd.DataFrame | None = None
        self._schedule_cache: pd.DataFrame | None = None

    # -------------------------------------------------------------------------
    # Player ID lookup
    # -------------------------------------------------------------------------

    def find_player_id(self, player_name: str) -> int | None:
        if player_name in self._player_id_cache:
            pid = self._player_id_cache[player_name]
            return int(pid) if pid else None

        try:
            roster = self._get_roster()
            if roster.empty:
                return None

            name_lower = player_name.lower()
            # Exact match first
            mask = roster["player_name"].fillna("").astype(str).str.lower() == name_lower
            match = roster[mask]

            if match.empty:
                # Partial match (last name)
                last = player_name.strip().split()[-1].lower()
                mask2 = roster["player_name"].fillna("").astype(str).str.lower().str.endswith(last)
                match = roster[mask2]

            if match.empty:
                logger.warning("NFL: no results for player '%s'", player_name)
                self._player_id_cache[player_name] = None
                return None

            pid = str(match.iloc[0]["player_id"])
            self._player_id_cache[player_name] = pid
            return int(pid) if pid.isdigit() else hash(pid) % (10 ** 9)
        except Exception as exc:
            logger.warning("NFL player lookup failed for '%s': %s", player_name, exc)
            self._player_id_cache[player_name] = None
            return None

    def _get_roster(self) -> pd.DataFrame:
        if self._roster_cache is not None:
            return self._roster_cache
        try:
            import nfl_data_py as nfl  # type: ignore[import]
            year = date.today().year
            roster = nfl.import_seasonal_rosters([year])
            self._roster_cache = roster if not roster.empty else pd.DataFrame()
        except ImportError:
            logger.warning(
                "nfl_data_py not installed. Run: pip install nfl-data-py\n"
                "NFL props will be skipped."
            )
            self._roster_cache = pd.DataFrame()
        except Exception as exc:
            logger.warning("NFL roster fetch failed: %s", exc)
            self._roster_cache = pd.DataFrame()
        return self._roster_cache

    # -------------------------------------------------------------------------
    # Game log
    # -------------------------------------------------------------------------

    def get_player_game_log(self, player_id: int) -> pd.DataFrame:
        if player_id in self._weekly_cache:
            return self._weekly_cache[player_id]

        df = self._build_player_log(player_id)

        # Persist to archive and enrich with full history.
        if df is not None and not df.empty:
            _ARCHIVE.merge("NFL", player_id, df)
        df = _merge_with_archive(player_id, df)

        self._weekly_cache[player_id] = df
        return df

    def _build_player_log(self, player_id: int) -> pd.DataFrame:
        try:
            import nfl_data_py as nfl  # type: ignore[import]
            year = date.today().year
            weekly = nfl.import_weekly_data([year])
            if weekly.empty:
                return pd.DataFrame()

            # nfl_data_py uses string player IDs
            pid_str = str(player_id)
            player_data = weekly[weekly["player_id"].astype(str) == pid_str]

            if player_data.empty:
                return pd.DataFrame()

            schedule = self._get_schedule(year)
            rows = []
            for _, row in player_data.iterrows():
                week = row.get("week", 0)
                game_date = self._week_to_date(schedule, week, year)
                team = str(row.get("recent_team", ""))
                opp = str(row.get("opponent_team", ""))
                is_home = row.get("home_team", "") == team

                rows.append({
                    "GAME_DATE": game_date,
                    "MATCHUP": f"{team} {'vs.' if is_home else '@'} {opp}",
                    "location": "Home" if is_home else "Away",
                    "opponent_abbr": opp,
                    "PASS_YDS": float(row.get("passing_yards", 0) or 0),
                    "PASS_TDS": float(row.get("passing_tds", 0) or 0),
                    "RUSH_YDS": float(row.get("rushing_yards", 0) or 0),
                    "REC":      float(row.get("receptions", 0) or 0),
                    "REC_YDS": float(row.get("receiving_yards", 0) or 0),
                })

            if not rows:
                return pd.DataFrame()

            df = pd.DataFrame(rows)
            df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"], errors="coerce")
            df = df.dropna(subset=["GAME_DATE"])
            return df.sort_values("GAME_DATE", ascending=False).reset_index(drop=True)

        except ImportError:
            return pd.DataFrame()
        except Exception as exc:
            logger.warning("NFL game log build failed for player %d: %s", player_id, exc)
            return pd.DataFrame()

    def _get_schedule(self, year: int) -> pd.DataFrame:
        if self._schedule_cache is not None:
            return self._schedule_cache
        try:
            import nfl_data_py as nfl  # type: ignore[import]
            sched = nfl.import_schedules([year])
            self._schedule_cache = sched
        except Exception:
            self._schedule_cache = pd.DataFrame()
        return self._schedule_cache

    @staticmethod
    def _week_to_date(schedule: pd.DataFrame, week: int, year: int):
        if schedule.empty or "week" not in schedule.columns:
            return pd.NaT
        wk = schedule[schedule["week"] == week]
        if wk.empty:
            return pd.NaT
        col = next((c for c in ("gameday", "game_date", "date") if c in wk.columns), None)
        if col is None:
            return pd.NaT
        return pd.to_datetime(wk[col].iloc[0], errors="coerce")

    # -------------------------------------------------------------------------
    # Team stats
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
                for col in ("PASS_YDS", "PASS_TDS", "RUSH_YDS", "REC", "REC_YDS")
                if col in row.index
            }
            stats["played"] = True
            return stats
        except Exception as exc:
            logger.warning("NFL get_game_stats_for_date failed (player %d): %s", player_id, exc)
            return None
