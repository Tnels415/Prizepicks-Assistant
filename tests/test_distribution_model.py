"""Tests for analysis.distribution_model — P(over) across NB/Poisson/Gaussian."""
from __future__ import annotations

import pandas as pd
import pytest

from analysis import distribution_model as dm


def _series(values):
    return pd.Series([float(v) for v in values])


class TestProbOver:
    def test_too_short_returns_none(self):
        assert dm.prob_over("Points", 20.0, 22.0, _series([20, 21, 22, 23])) is None

    def test_nonpositive_mean_returns_none(self):
        assert dm.prob_over("Points", 5.0, 0.0, _series([1, 2, 3, 4, 5])) is None

    def test_returns_complementary_pair(self):
        out = dm.prob_over("Points", 20.0, 25.0, _series([20, 22, 25, 28, 30, 24]))
        assert out is not None
        p_over, p_under = out
        assert p_over + p_under == pytest.approx(100.0)
        assert 0.0 <= p_over <= 100.0

    def test_mean_above_line_gives_over_majority(self):
        # Projected mean comfortably above line → P(over) > 50%
        out = dm.prob_over("Points", 18.0, 26.0, _series([24, 25, 26, 27, 28, 26]))
        assert out is not None
        assert out[0] > 50.0

    def test_mean_below_line_gives_under_majority(self):
        out = dm.prob_over("Points", 30.0, 22.0, _series([20, 21, 22, 23, 24, 22]))
        assert out is not None
        assert out[0] < 50.0


class TestDistributionSelection:
    def test_continuous_stat_uses_gaussian(self):
        assert dm._choose_distribution("Passing Yards", 2.0) == "gaussian"

    def test_small_count_uses_negbinom(self):
        assert dm._choose_distribution("Assists", 3.0) == "negbinom"

    def test_large_count_uses_gaussian(self):
        assert dm._choose_distribution("Points", 25.0) == "gaussian"


class TestNegBinomVsPoisson:
    def test_underdispersed_falls_back_to_poisson(self):
        # variance < mean → Poisson path; should still return a valid prob
        s = _series([3, 3, 3, 3, 4, 3])  # low variance
        p = dm._negbinom_prob_over(2.0, 3.0, s)
        assert p is not None
        assert 0.0 <= p <= 1.0

    def test_poisson_prob_over_monotonic_in_line(self):
        # Higher line → lower P(over)
        low = dm._poisson_prob_over(1.0, 3.0)
        high = dm._poisson_prob_over(5.0, 3.0)
        assert low > high
