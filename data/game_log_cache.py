from __future__ import annotations

"""Disk-based game log cache.

Stores one CSV file per player per sport under cache/game_logs/{sport}/{player_id}.csv.
Cache entries are valid for 24 hours; stale entries (>24h but <7 days) can be
retrieved explicitly for fallback purposes.
"""

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 24 * 3600        # 24 hours — normal TTL
_CACHE_STALE_SECONDS = 7 * 24 * 3600  # 7 days  — stale-but-usable TTL
_CACHE_ROOT = Path("cache/game_logs")


class GameLogCache:
    """Simple disk cache for player game-log DataFrames."""

    def __init__(self, root: Path | str = _CACHE_ROOT) -> None:
        self._root = Path(root)

    def _path(self, sport: str, player_id: int) -> Path:
        return self._root / sport / f"{player_id}.csv"

    def _age_seconds(self, path: Path) -> float:
        """Return the age of *path* in seconds, or infinity if it doesn't exist."""
        try:
            mtime = path.stat().st_mtime
            return time.time() - mtime
        except FileNotFoundError:
            return float("inf")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, sport: str, player_id: int) -> pd.DataFrame | None:
        """Return cached DataFrame if it exists and is < 24 hours old, else None."""
        path = self._path(sport, player_id)
        age = self._age_seconds(path)
        if age >= _CACHE_TTL_SECONDS:
            if age < float("inf"):
                logger.debug(
                    "Cache miss (expired %.1fh old) for %s player %d",
                    age / 3600, sport, player_id,
                )
            return None
        try:
            df = pd.read_csv(path, parse_dates=["GAME_DATE"])
            logger.debug(
                "Cache hit (%.1fh old, %d rows) for %s player %d",
                age / 3600, len(df), sport, player_id,
            )
            return df
        except Exception as exc:
            logger.debug("Cache read failed for %s player %d: %s", sport, player_id, exc)
            return None

    def get_stale(self, sport: str, player_id: int) -> pd.DataFrame | None:
        """Return cached DataFrame if it exists and is < 7 days old (even if expired).

        Used as a last-resort fallback when both live API and BallDontLie fail.
        """
        path = self._path(sport, player_id)
        age = self._age_seconds(path)
        if age >= _CACHE_STALE_SECONDS:
            return None
        try:
            df = pd.read_csv(path, parse_dates=["GAME_DATE"])
            logger.warning(
                "Using stale cache (%.1fh old, %d rows) for %s player %d — "
                "live API failed and no fresh data is available",
                age / 3600, len(df), sport, player_id,
            )
            return df
        except Exception as exc:
            logger.debug("Stale cache read failed for %s player %d: %s", sport, player_id, exc)
            return None

    def invalidate(self, sport: str, player_id: int) -> None:
        """Remove a player's cache entry so the next get() fetches fresh data."""
        path = self._path(sport, player_id)
        try:
            path.unlink(missing_ok=True)
            logger.debug("Cache invalidated for %s player %d", sport, player_id)
        except Exception as exc:
            logger.debug("Cache invalidate failed for %s player %d: %s", sport, player_id, exc)

    def set(self, sport: str, player_id: int, df: pd.DataFrame) -> None:
        """Write *df* to disk as a CSV under cache/game_logs/{sport}/{player_id}.csv."""
        if df is None or df.empty:
            return
        path = self._path(sport, player_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Serialize GAME_DATE as ISO-format strings so read_csv can parse them back.
            out = df.copy()
            if "GAME_DATE" in out.columns:
                out["GAME_DATE"] = pd.to_datetime(out["GAME_DATE"]).dt.strftime("%Y-%m-%dT%H:%M:%S")
            out.to_csv(path, index=False)
            logger.debug("Cache write (%d rows) for %s player %d", len(df), sport, player_id)
        except Exception as exc:
            logger.debug("Cache write failed for %s player %d: %s", sport, player_id, exc)
