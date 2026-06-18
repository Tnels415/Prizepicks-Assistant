"""
Offline backtest / calibration analytics over recorded predictions.

"Is the correct rate too low?" is unanswerable without measurement.  These pure
functions take a list of evaluated prediction dicts (as stored in history.db:
each has hit_probability, correct, direction, stat_type, raw_adjustments, …) and
produce the diagnostics needed to actually tune the model:

  - Brier score & log loss      → overall probabilistic accuracy
  - Calibration table           → are 70% picks hitting ~70%?
  - Hit rate vs break-even      → per-leg profitability by confidence band
  - Per-factor lift             → which signals actually predict outcomes
  - Market-anchor agreement     → is the devigged market beating the model?

All functions are side-effect free so they can be unit-tested and reused by the
backtest.py CLI.  `correct` is 0/1; `hit_probability` is the percent chance of
the bet side (already direction-aware as stored).
"""
from __future__ import annotations

import math

_EPS = 1e-9


def _clamp01(p: float) -> float:
    return max(_EPS, min(1.0 - _EPS, p))


def brier_score(preds: list[dict]) -> float | None:
    """Mean squared error between predicted probability and outcome. Lower is better.

    0.25 is the score of always guessing 50%; below that means real signal.
    """
    rows = [p for p in preds if p.get("correct") in (0, 1)]
    if not rows:
        return None
    total = sum((p["hit_probability"] / 100.0 - p["correct"]) ** 2 for p in rows)
    return total / len(rows)


def log_loss(preds: list[dict]) -> float | None:
    """Mean negative log-likelihood. Punishes confident wrong picks heavily."""
    rows = [p for p in preds if p.get("correct") in (0, 1)]
    if not rows:
        return None
    total = 0.0
    for p in rows:
        prob = _clamp01(p["hit_probability"] / 100.0)
        y = p["correct"]
        total += -(y * math.log(prob) + (1 - y) * math.log(1.0 - prob))
    return total / len(rows)


def calibration_table(preds: list[dict], n_buckets: int = 10) -> list[dict]:
    """Bucket predictions by predicted probability and compare to actual hit rate.

    Returns one row per non-empty bucket: predicted-mean, actual-rate, n, gap.
    A well-calibrated model has actual ≈ predicted in every bucket.
    """
    rows = [p for p in preds if p.get("correct") in (0, 1)]
    buckets: dict[int, list[dict]] = {}
    width = 100.0 / n_buckets
    for p in rows:
        idx = min(n_buckets - 1, int(p["hit_probability"] // width))
        buckets.setdefault(idx, []).append(p)

    table = []
    for idx in sorted(buckets):
        group = buckets[idx]
        n = len(group)
        pred_mean = sum(g["hit_probability"] for g in group) / n
        actual = sum(g["correct"] for g in group) / n * 100.0
        table.append({
            "bucket": f"{idx * width:.0f}-{(idx + 1) * width:.0f}%",
            "n": n,
            "predicted": round(pred_mean, 1),
            "actual": round(actual, 1),
            "gap": round(actual - pred_mean, 1),
        })
    return table


def hit_rate_vs_breakeven(
    preds: list[dict], breakeven_pct: float, min_prob: float = 50.0, step: float = 5.0
) -> list[dict]:
    """Per-leg profitability by confidence threshold.

    For each threshold, report the realized hit rate of all picks at or above it
    and whether that clears the per-leg break-even.  This is the honest per-leg
    ROI proxy: a band that hits above break-even is profitable; below it bleeds.
    """
    rows = [p for p in preds if p.get("correct") in (0, 1)]
    out = []
    threshold = min_prob
    while threshold <= 95.0:
        band = [p for p in rows if p["hit_probability"] >= threshold]
        if band:
            hr = sum(p["correct"] for p in band) / len(band) * 100.0
            out.append({
                "threshold": round(threshold, 0),
                "n": len(band),
                "hit_rate": round(hr, 1),
                "vs_breakeven": round(hr - breakeven_pct, 1),
            })
        threshold += step
    return out


def factor_lift(preds: list[dict], factor_names: list[str]) -> list[dict]:
    """For each factor, compare hit rate when it leaned the bet's way vs against.

    A predictive factor should show a positive 'lift' — picks where the factor
    agreed with the bet direction hit more often than picks where it disagreed.
    Factors with near-zero or negative lift are noise (or wired backwards).
    """
    rows = [p for p in preds if p.get("correct") in (0, 1) and p.get("raw_adjustments")]
    out = []
    for factor in factor_names:
        agree: list[int] = []
        disagree: list[int] = []
        for p in rows:
            val = p["raw_adjustments"].get(factor)
            if val is None or abs(val) < 0.5:
                continue
            # A positive factor delta pushes toward OVER; align with bet side.
            leans_over = val > 0
            bet_over = p["direction"] == "OVER"
            (agree if leans_over == bet_over else disagree).append(p["correct"])
        if not agree or not disagree:
            continue
        hr_agree = sum(agree) / len(agree) * 100.0
        hr_disagree = sum(disagree) / len(disagree) * 100.0
        out.append({
            "factor": factor,
            "n_agree": len(agree),
            "n_disagree": len(disagree),
            "hr_agree": round(hr_agree, 1),
            "hr_disagree": round(hr_disagree, 1),
            "lift": round(hr_agree - hr_disagree, 1),
        })
    out.sort(key=lambda r: r["lift"], reverse=True)
    return out


def market_agreement(preds: list[dict]) -> dict | None:
    """How often the devigged market (entry) agreed with the bet, and its accuracy.

    Uses _market_over stored in raw_adjustments at pick time.  If the market is a
    better predictor than the model, leaning harder on the anchor is warranted.
    """
    rows = [
        p for p in preds
        if p.get("correct") in (0, 1)
        and p.get("raw_adjustments")
        and p["raw_adjustments"].get("_market_over") is not None
    ]
    if not rows:
        return None
    market_correct = 0
    for p in rows:
        m_over = p["raw_adjustments"]["_market_over"]
        market_side_over = m_over >= 50.0
        bet_over = p["direction"] == "OVER"
        # Did the market's lean match the actual outcome for this bet?
        # correct==1 means the bet won; market "agreed" when its lean matched the bet.
        agreed = market_side_over == bet_over
        if agreed == bool(p["correct"]):
            market_correct += 1
    return {
        "n": len(rows),
        "market_accuracy": round(market_correct / len(rows) * 100, 1),
    }
