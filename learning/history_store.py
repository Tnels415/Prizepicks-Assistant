from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

DB_PATH = Path("history.db")

# Canonical factor order — must match the adjustments list in prop_analyzer.py
FACTOR_NAMES = [
    "season_avg_vs_line",
    "recent_form",
    "h2h_this_season",
    "opponent_defense",
    "home_away",
    "rest_days",
    "pace",
]

_DDL = """
CREATE TABLE IF NOT EXISTS predictions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    date             TEXT    NOT NULL,
    player_name      TEXT    NOT NULL,
    player_id        INTEGER,
    team_abbr        TEXT    NOT NULL,
    stat_type        TEXT    NOT NULL,
    line             REAL    NOT NULL,
    direction        TEXT    NOT NULL,
    hit_probability  REAL    NOT NULL,
    rank             INTEGER NOT NULL,
    predicted_value  REAL,
    raw_adjustments  TEXT,
    actual_value     REAL,
    correct          INTEGER,
    evaluated_at     TEXT,
    UNIQUE(date, player_name, stat_type, direction)
);
CREATE INDEX IF NOT EXISTS idx_predictions_date
    ON predictions(date);

CREATE TABLE IF NOT EXISTS factor_weights (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    date                  TEXT    NOT NULL,
    factor_name           TEXT    NOT NULL,
    weight                REAL    NOT NULL,
    accuracy_contribution REAL    NOT NULL,
    sample_size           INTEGER NOT NULL,
    UNIQUE(date, factor_name)
);
CREATE INDEX IF NOT EXISTS idx_factor_weights_date
    ON factor_weights(date);
"""


class HistoryStore:

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path
        self._migrate()

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self._db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _migrate(self) -> None:
        with self._conn() as conn:
            for stmt in _DDL.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    conn.execute(stmt)

    # ------------------------------------------------------------------
    # Write predictions
    # ------------------------------------------------------------------

    def save_predictions(
        self,
        results: list,
        run_date: date,
        player_id_map: dict[str, int | None],
    ) -> int:
        date_str = run_date.isoformat()
        rows = []
        for r in results:
            rows.append((
                date_str,
                r.player_name,
                player_id_map.get(r.player_name),
                r.team_abbr,
                r.stat_type,
                r.line,
                r.direction,
                r.hit_probability,
                r.rank,
                r.predicted_value,
                json.dumps(r.raw_adjustments),
            ))
        inserted = 0
        with self._conn() as conn:
            for row in rows:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO predictions
                       (date, player_name, player_id, team_abbr, stat_type, line,
                        direction, hit_probability, rank, predicted_value, raw_adjustments)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    row,
                )
                inserted += cur.rowcount
        logger.debug("Saved %d/%d predictions for %s", inserted, len(rows), date_str)
        return inserted

    def update_player_id(self, player_name: str, player_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE predictions SET player_id=? WHERE player_name=? AND player_id IS NULL",
                (player_id, player_name),
            )

    # ------------------------------------------------------------------
    # Read unevaluated predictions
    # ------------------------------------------------------------------

    def get_unevaluated_predictions(self, for_date: date) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM predictions WHERE date=? AND correct IS NULL",
                (for_date.isoformat(),),
            ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d.get("raw_adjustments"):
                try:
                    d["raw_adjustments"] = json.loads(d["raw_adjustments"])
                except Exception:
                    d["raw_adjustments"] = {}
            result.append(d)
        return result

    def get_distinct_unevaluated_players(self, for_date: date) -> list[tuple[str, int | None]]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT DISTINCT player_name, player_id
                   FROM predictions WHERE date=? AND correct IS NULL""",
                (for_date.isoformat(),),
            ).fetchall()
        return [(r["player_name"], r["player_id"]) for r in rows]

    # ------------------------------------------------------------------
    # Write outcomes
    # ------------------------------------------------------------------

    def record_outcome(
        self,
        prediction_id: int,
        actual_value: float,
        correct: int,
    ) -> None:
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                """UPDATE predictions
                   SET actual_value=?, correct=?, evaluated_at=?
                   WHERE id=?""",
                (actual_value, correct, now, prediction_id),
            )

    # ------------------------------------------------------------------
    # Read for calibration
    # ------------------------------------------------------------------

    def get_evaluated_predictions(self, min_days: int = 7) -> list[dict]:
        if self.count_distinct_dates() < min_days:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM predictions WHERE correct IN (0, 1)",
            ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d.get("raw_adjustments"):
                try:
                    d["raw_adjustments"] = json.loads(d["raw_adjustments"])
                except Exception:
                    d["raw_adjustments"] = {}
            result.append(d)
        return result

    def count_distinct_dates(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT date) AS n FROM predictions WHERE correct IN (0,1)"
            ).fetchone()
        return row["n"] if row else 0

    # ------------------------------------------------------------------
    # Factor weights
    # ------------------------------------------------------------------

    def save_factor_weights(
        self,
        weights: dict[str, float],
        accuracy_contributions: dict[str, float],
        sample_sizes: dict[str, int],
        snapshot_date: date,
    ) -> None:
        date_str = snapshot_date.isoformat()
        with self._conn() as conn:
            for factor in FACTOR_NAMES:
                conn.execute(
                    """INSERT OR REPLACE INTO factor_weights
                       (date, factor_name, weight, accuracy_contribution, sample_size)
                       VALUES (?,?,?,?,?)""",
                    (
                        date_str,
                        factor,
                        weights.get(factor, 1.0),
                        accuracy_contributions.get(factor, 0.5),
                        sample_sizes.get(factor, 0),
                    ),
                )

    def get_latest_factor_weights(self) -> dict[str, float] | None:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT factor_name, weight FROM factor_weights
                   WHERE date = (SELECT MAX(date) FROM factor_weights)"""
            ).fetchall()
        if not rows:
            return None
        return {r["factor_name"]: r["weight"] for r in rows}

    # ------------------------------------------------------------------
    # Email section data
    # ------------------------------------------------------------------

    def get_results_for_date(self, for_date: date) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM predictions WHERE date=? ORDER BY rank ASC",
                (for_date.isoformat(),),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_cumulative_accuracy(self) -> dict:
        with self._conn() as conn:
            total_row = conn.execute(
                "SELECT COUNT(*) AS n, SUM(correct) AS s FROM predictions WHERE correct IN (0,1)"
            ).fetchone()
            stat_rows = conn.execute(
                """SELECT stat_type,
                          COUNT(*) AS total,
                          SUM(correct) AS correct
                   FROM predictions WHERE correct IN (0,1)
                   GROUP BY stat_type"""
            ).fetchall()
            dir_rows = conn.execute(
                """SELECT direction,
                          COUNT(*) AS total,
                          SUM(correct) AS correct
                   FROM predictions WHERE correct IN (0,1)
                   GROUP BY direction"""
            ).fetchall()
            days_row = conn.execute(
                "SELECT COUNT(DISTINCT date) AS n FROM predictions WHERE correct IN (0,1)"
            ).fetchone()

        total = total_row["n"] or 0
        correct = int(total_row["s"] or 0)
        return {
            "total_evaluated": total,
            "total_correct": correct,
            "accuracy_pct": round(correct / total * 100, 1) if total else 0.0,
            "by_stat_type": {
                r["stat_type"]: {
                    "correct": int(r["correct"] or 0),
                    "total": r["total"],
                    "pct": round(int(r["correct"] or 0) / r["total"] * 100, 1),
                }
                for r in stat_rows
            },
            "by_direction": {
                r["direction"]: {
                    "correct": int(r["correct"] or 0),
                    "total": r["total"],
                    "pct": round(int(r["correct"] or 0) / r["total"] * 100, 1),
                }
                for r in dir_rows
            },
            "days_of_data": days_row["n"] if days_row else 0,
        }
