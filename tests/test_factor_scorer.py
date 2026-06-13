"""Tests for analysis.factor_scorer — bounds and directional correctness."""
from __future__ import annotations

import pytest

from analysis.factor_scorer import FactorScorer


@pytest.fixture
def scorer():
    return FactorScorer()


class TestSeasonAvgVsLine:
    def test_clamped(self, scorer):
        assert -10.0 <= scorer.score_season_avg_vs_line(100.0, 1.0) <= 10.0
        assert scorer.score_season_avg_vs_line(50.0, 1.0) == pytest.approx(10.0)

    def test_direction(self, scorer):
        assert scorer.score_season_avg_vs_line(30.0, 20.0) > 0
        assert scorer.score_season_avg_vs_line(15.0, 20.0) < 0

    def test_zero_line_safe(self, scorer):
        assert scorer.score_season_avg_vs_line(20.0, 0.0) == 0.0


class TestRestDays:
    def test_back_to_back_negative(self, scorer):
        assert scorer.score_rest_days(0) < 0

    def test_rested_positive(self, scorer):
        assert scorer.score_rest_days(3) > 0


class TestCompositeProbability:
    def test_clamped_to_5_95(self, scorer):
        assert scorer.compute_composite_probability(1.0, [50, 50, 50]) <= 95.0
        assert scorer.compute_composite_probability(0.0, [-50, -50, -50]) >= 5.0

    def test_base_prob_overrides_hit_rate(self, scorer):
        # With no adjustments, base_prob should pass through unchanged.
        out = scorer.compute_composite_probability(0.5, [0, 0], base_prob=72.0)
        assert out == pytest.approx(72.0)

    def test_adjustments_move_probability(self, scorer):
        up = scorer.compute_composite_probability(0.5, [5.0], base_prob=60.0)
        down = scorer.compute_composite_probability(0.5, [-5.0], base_prob=60.0)
        assert up > 60.0 > down


class TestAnalystSentiment:
    def test_bounds(self, scorer):
        assert scorer.score_analyst_sentiment(1.0) == pytest.approx(4.0)
        assert scorer.score_analyst_sentiment(-1.0) == pytest.approx(-4.0)
        assert scorer.score_analyst_sentiment(0.0) == 0.0
