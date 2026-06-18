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
    # Per-player historical signal factors
    "consistency",
    "hit_rate_trend",
    "trend_direction",
    # External analyst sentiment (ESPN news keyword analysis)
    "analyst_sentiment",
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
    sport            TEXT    NOT NULL DEFAULT 'NBA',
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
    sport                 TEXT    NOT NULL DEFAULT 'NBA',
    UNIQUE(date, factor_name, sport)
);
CREATE INDEX IF NOT EXISTS idx_factor_weights_date
    ON factor_weights(date);
"""

# Migration to add 'sport' column to existing databases
_MIGRATIONS = [
    "ALTER TABLE predictions ADD COLUMN sport TEXT NOT NULL DEFAULT 'NBA'",
    "ALTER TABLE factor_weights ADD COLUMN sport TEXT NOT NULL DEFAULT 'NBA'",
    "CREATE INDEX IF NOT EXISTS idx_predictions_sport ON predictions(sport)",
    # Closing-line value (CLV): the consensus market line/probability captured
    # near game time.  closing_prob_over is P(over) in percent at close.
    "ALTER TABLE predictions ADD COLUMN closing_line REAL",
    "ALTER TABLE predictions ADD COLUMN closing_prob_over REAL",
    "ALTER TABLE predictions ADD COLUMN clv REAL",
]


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
            # Apply column-add migrations idempotently
            for sql in _MIGRATIONS:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass  # column already exists

    # ------------------------------------------------------------------
    # Write predictions
    # ------------------------------------------------------------------

    def save_predictions(
        self,
        results: list,
        run_date: date,
        player_id_map: dict[str, int | None],
        sport: str = "NBA",
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
                getattr(r, "sport", sport),
            ))
        inserted = 0
        with self._conn() as conn:
            for row in rows:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO predictions
                       (date, player_name, player_id, team_abbr, stat_type, line,
                        direction, hit_probability, rank, predicted_value, raw_adjustments, sport)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    row,
                )
                inserted += cur.rowcount
        logger.debug("Saved %d/%d predictions (%s) for %s", inserted, len(rows), sport, date_str)
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

    def get_unevaluated_predictions(self, for_date: date, sport: str | None = None) -> list[dict]:
        with self._conn() as conn:
            if sport:
                rows = conn.execute(
                    "SELECT * FROM predictions WHERE date=? AND correct IS NULL AND sport=?",
                    (for_date.isoformat(), sport),
                ).fetchall()
            else:
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

    def get_distinct_unevaluated_players(
        self,
        for_date: date,
        sport: str | None = None,
    ) -> list[tuple[str, int | None]]:
        with self._conn() as conn:
            if sport:
                rows = conn.execute(
                    """SELECT DISTINCT player_name, player_id
                       FROM predictions WHERE date=? AND correct IS NULL AND sport=?""",
                    (for_date.isoformat(), sport),
                ).fetchall()
            else:
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
    # Closing-line value (CLV)
    # ------------------------------------------------------------------

    def get_predictions_needing_closing_line(self, for_date: date) -> list[dict]:
        """Return predictions on for_date that have no closing line captured yet."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT id, player_name, stat_type, line, direction, hit_probability,
                          raw_adjustments, sport
                   FROM predictions
                   WHERE date=? AND closing_prob_over IS NULL""",
                (for_date.isoformat(),),
            ).fetchall()
        return [dict(r) for r in rows]

    def record_closing_line(
        self,
        prediction_id: int,
        direction: str,
        hit_probability: float,
        closing_line: float,
        closing_prob_over: float,
    ) -> None:
        """Store the closing line and compute CLV for one prediction.

        CLV is expressed in probability points on the side we bet: how much more
        likely the closing market said our pick was, versus what we entered at.
        Positive CLV means we beat the close — the single best leading indicator
        of long-term +EV, available immediately (no need to wait for the result).
        """
        closing_for_side = (
            closing_prob_over if direction == "OVER" else 100.0 - closing_prob_over
        )
        clv = closing_for_side - hit_probability
        with self._conn() as conn:
            conn.execute(
                """UPDATE predictions
                   SET closing_line=?, closing_prob_over=?, clv=?
                   WHERE id=?""",
                (closing_line, closing_prob_over, clv, prediction_id),
            )

    def get_clv_summary(self, sport: str | None = None) -> dict:
        """Aggregate CLV stats across all predictions that have a closing line."""
        with self._conn() as conn:
            where = "WHERE clv IS NOT NULL"
            args: tuple = ()
            if sport:
                where += " AND sport=?"
                args = (sport,)
            rows = conn.execute(
                f"SELECT clv, correct FROM predictions {where}", args
            ).fetchall()
        clvs = [r["clv"] for r in rows]
        if not clvs:
            return {"n": 0, "avg_clv": 0.0, "pct_beat_close": 0.0}
        beat = sum(1 for c in clvs if c > 0)
        return {
            "n": len(clvs),
            "avg_clv": round(sum(clvs) / len(clvs), 2),
            "pct_beat_close": round(beat / len(clvs) * 100, 1),
        }

    # ------------------------------------------------------------------
    # Read for calibration
    # ------------------------------------------------------------------

    def get_evaluated_predictions(self, min_days: int = 7, sport: str = "NBA") -> list[dict]:
        if self.count_distinct_dates(sport=sport) < min_days:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM predictions WHERE correct IN (0, 1) AND sport=?",
                (sport,),
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

    def get_all_evaluated_predictions(self, sport: str = "NBA") -> list[dict]:
        """Return all evaluated predictions for *sport* with no day-count gate.

        The calibrator applies shrinkage and time-decay, so no hard sample gate is
        needed here; every evaluated prediction contributes proportionally.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM predictions WHERE correct IN (0, 1) AND sport=?",
                (sport,),
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

    def get_player_accuracy(
        self, sport: str = "NBA", min_samples: int = 10
    ) -> dict[tuple, float]:
        """Return {(player_name, stat_type): bias_pp} for every player-stat pair
        with at least min_samples evaluated predictions.  bias_pp > 0 means the
        model has been systematically under-predicting; < 0 means over-predicting."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT player_name, stat_type,
                       AVG(
                           CASE WHEN direction='OVER'
                                THEN hit_probability / 100.0
                                ELSE 1.0 - hit_probability / 100.0
                           END
                       ) AS mean_pred,
                       AVG(CAST(correct AS REAL)) AS actual_rate,
                       COUNT(*) AS n
                FROM predictions
                WHERE correct IN (0, 1) AND sport = ?
                GROUP BY player_name, stat_type
                HAVING n >= ?
                """,
                (sport, min_samples),
            ).fetchall()
        return {
            (r["player_name"], r["stat_type"]): (r["actual_rate"] - r["mean_pred"]) * 100.0
            for r in rows
        }

    def count_distinct_dates(self, sport: str = "NBA") -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT date) AS n FROM predictions WHERE correct IN (0,1) AND sport=?",
                (sport,),
            ).fetchone()
        return row["n"] if row else 0

    # ------------------------------------------------------------------
    # Factor weights (per-sport)
    # ------------------------------------------------------------------

    def save_factor_weights(
        self,
        weights: dict[str, float],
        accuracy_contributions: dict[str, float],
        sample_sizes: dict[str, int],
        snapshot_date: date,
        sport: str = "NBA",
    ) -> None:
        date_str = snapshot_date.isoformat()
        with self._conn() as conn:
            for factor in FACTOR_NAMES:
                conn.execute(
                    """INSERT OR REPLACE INTO factor_weights
                       (date, factor_name, weight, accuracy_contribution, sample_size, sport)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        date_str,
                        factor,
                        weights.get(factor, 1.0),
                        accuracy_contributions.get(factor, 0.5),
                        sample_sizes.get(factor, 0),
                        sport,
                    ),
                )

    def get_latest_factor_weights(self, sport: str = "NBA") -> dict[str, float] | None:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT factor_name, weight FROM factor_weights
                   WHERE sport=? AND date = (SELECT MAX(date) FROM factor_weights WHERE sport=?)""",
                (sport, sport),
            ).fetchall()
        if not rows:
            return None
        return {r["factor_name"]: r["weight"] for r in rows}

    # ------------------------------------------------------------------
    # Email section data
    # ------------------------------------------------------------------

    def get_results_for_date(self, for_date: date, sport: str | None = None) -> list[dict]:
        with self._conn() as conn:
            if sport:
                rows = conn.execute(
                    "SELECT * FROM predictions WHERE date=? AND sport=? ORDER BY rank ASC",
                    (for_date.isoformat(), sport),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM predictions WHERE date=? ORDER BY rank ASC",
                    (for_date.isoformat(),),
                ).fetchall()
        return [dict(r) for r in rows]

    def get_cumulative_accuracy(self, sport: str | None = None) -> dict:
        with self._conn() as conn:
            if sport:
                where = "WHERE correct IN (0,1) AND sport=?"
                args = (sport,)
            else:
                where = "WHERE correct IN (0,1)"
                args = ()

            total_row = conn.execute(
                f"SELECT COUNT(*) AS n, SUM(correct) AS s FROM predictions {where}", args
            ).fetchone()
            stat_rows = conn.execute(
                f"""SELECT stat_type, COUNT(*) AS total, SUM(correct) AS correct
                    FROM predictions {where} GROUP BY stat_type""",
                args,
            ).fetchall()
            dir_rows = conn.execute(
                f"""SELECT direction, COUNT(*) AS total, SUM(correct) AS correct
                    FROM predictions {where} GROUP BY direction""",
                args,
            ).fetchall()
            days_row = conn.execute(
                f"SELECT COUNT(DISTINCT date) AS n FROM predictions {where}", args
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
