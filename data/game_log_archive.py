from __future__ import annotations

"""Permanent append-only game-log archive backed by SQLite.

Each row stores one player-game as a JSON payload, keyed by
(sport, player_id, game_date).  The archive accumulates across seasons and
process restarts, giving the analyzer multi-season history instead of the
current-season-only snapshots from the live APIs.

Usage (from a stats client, after a live fetch):
    archive = GameLogArchive()
    archive.merge("NBA", player_id, fresh_df)
    full_history = archive.get("NBA", player_id)  # all-time, newest first
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

import pandas as pd

logger = logging.getLogger(__name__)

_ARCHIVE_PATH = Path("cache/game_log_archive.db")

_DDL = """
CREATE TABLE IF NOT EXISTS game_logs (
    sport       TEXT    NOT NULL,
    player_id   INTEGER NOT NULL,
    game_date   TEXT    NOT NULL,
    payload     TEXT    NOT NULL,
    PRIMARY KEY (sport, player_id, game_date)
);
CREATE INDEX IF NOT EXISTS idx_game_logs_lookup
    ON game_logs (sport, player_id, game_date DESC);
"""


class GameLogArchive:
    """SQLite-backed permanent store for player game logs."""

    def __init__(self, path: Path | str = _ARCHIVE_PATH) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_DDL)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        con = sqlite3.connect(self._path, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def merge(self, sport: str, player_id: int, df: pd.DataFrame) -> int:
        """Upsert rows from *df* into the archive.

        Rows are keyed on (sport, player_id, game_date). Existing rows for the
        same key are overwritten (INSERT OR REPLACE) so corrected data from
        outcome evaluation can displace stale entries. Returns the number of
        rows written.
        """
        if df is None or df.empty or "GAME_DATE" not in df.columns:
            return 0

        rows = []
        for _, row in df.iterrows():
            gd = row["GAME_DATE"]
            if pd.isna(gd):
                continue
            date_str = pd.Timestamp(gd).strftime("%Y-%m-%d")
            payload = json.dumps(
                {
                    k: (None if pd.isna(v) else v)
                    for k, v in row.items()
                    if k != "GAME_DATE"
                },
                default=str,
            )
            rows.append((sport, player_id, date_str, payload))

        if not rows:
            return 0

        try:
            with self._connect() as con:
                con.executemany(
                    "INSERT OR REPLACE INTO game_logs (sport, player_id, game_date, payload) "
                    "VALUES (?, ?, ?, ?)",
                    rows,
                )
            logger.debug(
                "Archive: merged %d rows for %s player %d", len(rows), sport, player_id
            )
            return len(rows)
        except Exception as exc:
            logger.warning(
                "Archive: merge failed for %s player %d: %s", sport, player_id, exc
            )
            return 0

    def get(self, sport: str, player_id: int) -> pd.DataFrame | None:
        """Return full accumulated history for *player_id*, newest first.

        Returns None if no rows exist yet (signals callers to fall back to
        live API only and not assume empty history means the player is unknown).
        """
        try:
            with self._connect() as con:
                cur = con.execute(
                    "SELECT game_date, payload FROM game_logs "
                    "WHERE sport=? AND player_id=? ORDER BY game_date DESC",
                    (sport, player_id),
                )
                rows = cur.fetchall()
        except Exception as exc:
            logger.warning(
                "Archive: read failed for %s player %d: %s", sport, player_id, exc
            )
            return None

        if not rows:
            return None

        records = []
        for r in rows:
            entry = json.loads(r["payload"])
            entry["GAME_DATE"] = pd.Timestamp(r["game_date"])
            records.append(entry)

        df = pd.DataFrame(records)
        # Coerce numeric columns that may have been stored as strings via JSON.
        for col in df.columns:
            if col == "GAME_DATE":
                continue
            try:
                df[col] = pd.to_numeric(df[col], errors="ignore")
            except Exception:
                pass
        return df

    def count(self, sport: str, player_id: int) -> int:
        """Return number of stored game rows for *player_id*."""
        try:
            with self._connect() as con:
                cur = con.execute(
                    "SELECT COUNT(*) FROM game_logs WHERE sport=? AND player_id=?",
                    (sport, player_id),
                )
                return cur.fetchone()[0]
        except Exception:
            return 0
