"""Tests for analysis.backtest — calibration and diagnostic analytics."""
from __future__ import annotations

import math

import pytest

from analysis import backtest as bt


def _pred(prob, correct, direction="OVER", raw=None):
    return {
        "hit_probability": prob,
        "correct": correct,
        "direction": direction,
        "stat_type": "Points",
        "raw_adjustments": raw or {},
    }


class TestBrierAndLogLoss:
    def test_perfect_predictions_score_zero(self):
        preds = [_pred(99, 1), _pred(1, 0)]
        assert bt.brier_score(preds) < 0.001
        assert bt.log_loss(preds) < 0.05

    def test_coin_flip_is_quarter(self):
        preds = [_pred(50, 1), _pred(50, 0)]
        assert bt.brier_score(preds) == pytest.approx(0.25)

    def test_empty_returns_none(self):
        assert bt.brier_score([]) is None
        assert bt.log_loss([]) is None

    def test_ignores_ungraded(self):
        preds = [_pred(50, None), _pred(50, 1)]
        # Only the graded one counts: (0.5-1)^2 = 0.25
        assert bt.brier_score(preds) == pytest.approx(0.25)


class TestCalibrationTable:
    def test_buckets_and_gap(self):
        # 10 picks at 70%, 7 win → actual 70%, gap 0
        preds = [_pred(70, 1) for _ in range(7)] + [_pred(70, 0) for _ in range(3)]
        table = bt.calibration_table(preds)
        assert len(table) == 1
        row = table[0]
        assert row["n"] == 10
        assert row["predicted"] == pytest.approx(70.0)
        assert row["actual"] == pytest.approx(70.0)
        assert row["gap"] == pytest.approx(0.0)

    def test_overconfidence_shows_negative_gap(self):
        # Predicted 80%, only 50% hit → negative gap (overconfident)
        preds = [_pred(80, 1) for _ in range(5)] + [_pred(80, 0) for _ in range(5)]
        row = bt.calibration_table(preds)[0]
        assert row["gap"] < 0


class TestHitRateVsBreakeven:
    def test_threshold_filtering(self):
        preds = [_pred(60, 1), _pred(60, 0), _pred(80, 1), _pred(80, 1)]
        out = bt.hit_rate_vs_breakeven(preds, breakeven_pct=54.0, min_prob=60.0, step=20.0)
        by_thr = {r["threshold"]: r for r in out}
        # at >=80: 2 picks, both win → 100%
        assert by_thr[80.0]["hit_rate"] == pytest.approx(100.0)
        assert by_thr[80.0]["vs_breakeven"] == pytest.approx(46.0)


class TestFactorLift:
    def test_positive_lift_when_factor_predicts(self):
        # When season_avg_vs_line agrees with bet, picks win; when against, lose.
        preds = []
        for _ in range(10):
            preds.append(_pred(60, 1, "OVER", {"season_avg_vs_line": 5.0}))   # agree → win
        for _ in range(10):
            preds.append(_pred(60, 0, "OVER", {"season_avg_vs_line": -5.0}))  # disagree → loss
        out = bt.factor_lift(preds, ["season_avg_vs_line"])
        assert len(out) == 1
        assert out[0]["lift"] > 0


class TestMarketAgreement:
    def test_reports_accuracy(self):
        preds = [
            _pred(60, 1, "OVER", {"_market_over": 60.0}),   # market leans over, bet over, won → market right
            _pred(60, 0, "OVER", {"_market_over": 40.0}),   # market leans under, bet over, lost → market right
        ]
        out = bt.market_agreement(preds)
        assert out["n"] == 2
        assert out["market_accuracy"] == pytest.approx(100.0)

    def test_none_when_no_market_data(self):
        assert bt.market_agreement([_pred(60, 1)]) is None
