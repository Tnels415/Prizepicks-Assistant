from __future__ import annotations

"""
Backtest / calibration report over recorded predictions in history.db.

Usage:
    python3 backtest.py            # all sports combined
    python3 backtest.py --sport NBA

This reads every evaluated prediction (those with a known outcome) and prints
calibration, Brier/log-loss, per-leg profitability by confidence band, factor
lift, market agreement, and closing-line value (CLV).  Use it to decide what to
tune — e.g. if 70% picks only hit 60%, the model is overconfident; if a factor's
lift is negative, it's wired backwards or pure noise.
"""

import argparse

from analysis import backtest as bt
from learning.history_store import HistoryStore, FACTOR_NAMES
from config import PRIZEPICKS_BREAKEVEN


def _print_table(rows: list[dict], cols: list[tuple[str, str]]) -> None:
    if not rows:
        print("    (no data)")
        return
    header = "    " + "  ".join(f"{label:>12}" for _key, label in cols)
    print(header)
    print("    " + "-" * (len(header) - 4))
    for r in rows:
        print("    " + "  ".join(f"{str(r[key]):>12}" for key, _label in cols))


def main() -> int:
    parser = argparse.ArgumentParser(description="Prop model backtest report")
    parser.add_argument("--sport", default=None, help="Limit to one sport (NBA/NHL/MLB/NFL)")
    args = parser.parse_args()

    store = HistoryStore()
    if args.sport:
        preds = store.get_all_evaluated_predictions(sport=args.sport)
    else:
        # Combine every sport's evaluated predictions.
        preds = []
        for sport in ("NBA", "NHL", "MLB", "NFL"):
            preds.extend(store.get_all_evaluated_predictions(sport=sport))

    scope = args.sport or "ALL SPORTS"
    print("\n" + "=" * 60)
    print(f"  BACKTEST REPORT — {scope}")
    print("=" * 60)

    n = sum(1 for p in preds if p.get("correct") in (0, 1))
    if n == 0:
        print("\n  No evaluated predictions yet. Run the system for a few days,")
        print("  let outcomes be graded, then re-run this report.\n")
        return 0

    correct = sum(p["correct"] for p in preds if p.get("correct") in (0, 1))
    print(f"\n  Evaluated picks: {n}   |   Hit rate: {correct / n * 100:.1f}%")
    brier = bt.brier_score(preds)
    ll = bt.log_loss(preds)
    print(f"  Brier score: {brier:.4f} (0.25 = coin flip; lower is better)")
    print(f"  Log loss:    {ll:.4f}")

    print("\n  CALIBRATION (predicted vs actual by probability bucket)")
    _print_table(
        bt.calibration_table(preds),
        [("bucket", "Bucket"), ("n", "N"), ("predicted", "Pred%"),
         ("actual", "Actual%"), ("gap", "Gap")],
    )

    print(f"\n  PER-LEG PROFITABILITY (break-even = {PRIZEPICKS_BREAKEVEN * 100:.0f}%)")
    _print_table(
        bt.hit_rate_vs_breakeven(preds, PRIZEPICKS_BREAKEVEN * 100.0),
        [("threshold", "Prob>="), ("n", "N"), ("hit_rate", "HitRate%"),
         ("vs_breakeven", "vsBE")],
    )

    print("\n  FACTOR LIFT (hit rate when factor agrees with bet vs against)")
    _print_table(
        bt.factor_lift(preds, FACTOR_NAMES),
        [("factor", "Factor"), ("n_agree", "N+"), ("hr_agree", "HR+%"),
         ("hr_disagree", "HR-%"), ("lift", "Lift")],
    )

    market = bt.market_agreement(preds)
    if market:
        print(f"\n  MARKET ANCHOR: {market['n']} picks had a devigged market number; "
              f"market lean was correct {market['market_accuracy']}% of the time")

    clv = store.get_clv_summary(sport=args.sport)
    print("\n  CLOSING-LINE VALUE (CLV)")
    if clv["n"] == 0:
        print("    (no closing lines captured yet — run update_closing_lines.py near tip-off)")
    else:
        print(f"    {clv['n']} picks  |  avg CLV: {clv['avg_clv']:+.2f}pp  |  "
              f"beat the close: {clv['pct_beat_close']}%")
        print("    (positive avg CLV is the best leading indicator of long-term +EV)")

    print("\n" + "=" * 60 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
