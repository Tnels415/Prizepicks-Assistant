"""Tests for learning.calibrator — Brier score, shrinkage, and factor direction."""
from __future__ import annotations

from datetime import date

import pytest

from learning.calibrator import Calibrator


class TestBrierScore:
    def test_none_when_empty(self):
        assert Calibrator._compute_brier_score([]) is None

    def test_perfect_predictions_score_zero(self):
        rows = [
            {"hit_probability": 100.0, "correct": 1},
            {"hit_probability": 0.0, "correct": 0},
        ]
        assert Calibrator._compute_brier_score(rows) == pytest.approx(0.0)

    def test_coin_flip_scores_quarter(self):
        rows = [
            {"hit_probability": 50.0, "correct": 1},
            {"hit_probability": 50.0, "correct": 0},
        ]
        assert Calibrator._compute_brier_score(rows) == pytest.approx(0.25)


class TestFactorPointedCorrectly:
    def test_over_positive_delta_hit(self):
        assert Calibrator._factor_pointed_correctly(5.0, "OVER", 1) is True

    def test_over_positive_delta_miss(self):
        assert Calibrator._factor_pointed_correctly(5.0, "OVER", 0) is False

    def test_under_negative_delta_hit(self):
        # Negative delta predicts UNDER; if UNDER hit (correct==1) it pointed right.
        assert Calibrator._factor_pointed_correctly(-5.0, "UNDER", 1) is True

    def test_under_positive_delta_hit_is_wrong(self):
        assert Calibrator._factor_pointed_correctly(5.0, "UNDER", 1) is False


class TestDecayWeight:
    def test_today_full_weight(self):
        from learning.calibrator import _decay_weight
        row = {"date": date.today().isoformat()}
        assert _decay_weight(row, date.today()) == pytest.approx(1.0)

    def test_half_life(self):
        from learning.calibrator import _decay_weight, _HALF_LIFE_DAYS
        from datetime import timedelta
        old = (date.today() - timedelta(days=_HALF_LIFE_DAYS)).isoformat()
        assert _decay_weight({"date": old}, date.today()) == pytest.approx(0.5)
