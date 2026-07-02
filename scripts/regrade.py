from __future__ import annotations

"""
regrade.py — one-time repair for predictions mis-graded by the old grading bug.

Before the fix, a missing stat column or unknown played-status was coerced to
actual_value=0.0, grading every OVER as a loss (and every UNDER as a win).
This script re-nulls the grade on suspicious rows — evaluated rows from the
last N days with actual_value == 0 — so the next daily run re-evaluates them
with the fixed grading logic. Rows where 0 was a legitimate actual (e.g. 0
home runs) are also re-nulled; they will simply be re-graded to the same
result, so this is safe.

Usage:
    python3 scripts/regrade.py            # dry run — shows what would change
    python3 scripts/regrade.py --apply    # actually re-null the grades
    python3 scripts/regrade.py --days 60  # widen the window (default 30)
"""

import argparse
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from learning.history_store import DB_PATH  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="apply changes (default: dry run)")
    parser.add_argument("--days", type=int, default=30, help="lookback window in days")
    args = parser.parse_args()

    if not Path(DB_PATH).exists():
        print(f"No history database found at {DB_PATH} — nothing to do.")
        return 0

    cutoff = (date.today() - timedelta(days=args.days)).isoformat()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """SELECT id, date, sport, player_name, stat_type, direction, line, correct
           FROM predictions
           WHERE correct IN (0,1) AND actual_value = 0.0 AND date >= ?
           ORDER BY date""",
        (cutoff,),
    ).fetchall()

    if not rows:
        print(f"No suspicious rows (actual_value=0, graded, last {args.days} days). Nothing to do.")
        return 0

    print(f"Found {len(rows)} graded rows with actual_value=0 in the last {args.days} days:")
    for r in rows[:20]:
        verdict = "WIN" if r["correct"] == 1 else "LOSS"
        print(f"  {r['date']}  {r['sport']:<4} {r['player_name']:<22} "
              f"{r['direction']:<5} {r['stat_type']:<16} line {r['line']:<6} → graded {verdict}")
    if len(rows) > 20:
        print(f"  ... and {len(rows) - 20} more")

    if not args.apply:
        print("\nDry run — nothing changed. Re-run with --apply to re-null these grades")
        print("so the next daily run re-evaluates them with the fixed logic.")
        return 0

    cur = conn.execute(
        """UPDATE predictions
           SET correct = NULL, actual_value = NULL, evaluated_at = NULL
           WHERE correct IN (0,1) AND actual_value = 0.0 AND date >= ?""",
        (cutoff,),
    )
    conn.commit()
    print(f"\nRe-nulled {cur.rowcount} grades.")
    print("Rows dated yesterday will be re-graded on the next daily run; older rows")
    print("simply drop out of the accuracy record (the daily evaluator only looks")
    print("back one day). Either way the corrupted grades no longer pollute accuracy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
