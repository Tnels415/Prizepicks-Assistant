"""Tests for analysis.usage_boost — teammate-injury usage multiplier."""
from __future__ import annotations

import pytest

from analysis import usage_boost
from config import (
    SPORT_CONFIG, USAGE_BOOST_PER_TEAMMATE_OUT, USAGE_BOOST_MAX,
    USAGE_BOOST_DOUBTFUL_WEIGHT,
)

NBA = SPORT_CONFIG["NBA"]


class TestWeightedOutCount:
    def test_out_counts_full(self):
        assert usage_boost.weighted_out_count([("A", "out"), ("B", "out")]) == 2.0

    def test_doubtful_counts_partial(self):
        assert usage_boost.weighted_out_count(
            [("A", "doubtful")]
        ) == pytest.approx(USAGE_BOOST_DOUBTFUL_WEIGHT)

    def test_mixed(self):
        out = usage_boost.weighted_out_count([("A", "out"), ("B", "doubtful")])
        assert out == pytest.approx(1.0 + USAGE_BOOST_DOUBTFUL_WEIGHT)

    def test_empty(self):
        assert usage_boost.weighted_out_count([]) == 0.0


class TestUsageBoostMultiplier:
    def test_no_teammates_is_identity(self):
        assert usage_boost.usage_boost_multiplier([], "Points", NBA) == 1.0

    def test_counting_stat_boosted(self):
        m = usage_boost.usage_boost_multiplier([("A", "out")], "Points", NBA)
        assert m == pytest.approx(1.0 + USAGE_BOOST_PER_TEAMMATE_OUT)

    def test_non_counting_stat_not_boosted(self):
        # "Turnovers" is a counting stat; pick something that is NOT in counting_stats.
        m = usage_boost.usage_boost_multiplier([("A", "out")], "Personal Fouls", NBA)
        # Personal Fouls is not in NBA counting_stats → no boost
        assert m == 1.0

    def test_capped(self):
        many = [("P%d" % i, "out") for i in range(20)]
        m = usage_boost.usage_boost_multiplier(many, "Points", NBA)
        assert m == pytest.approx(1.0 + USAGE_BOOST_MAX)

    def test_doubtful_half_weight(self):
        m = usage_boost.usage_boost_multiplier([("A", "doubtful")], "Points", NBA)
        expected = 1.0 + USAGE_BOOST_DOUBTFUL_WEIGHT * USAGE_BOOST_PER_TEAMMATE_OUT
        assert m == pytest.approx(expected)
