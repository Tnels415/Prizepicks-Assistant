"""Tests for analysis.market — American-odds conversion, de-vig, and blending."""
from __future__ import annotations

import math

import pytest

from analysis import market


class TestAmericanToProb:
    def test_even_money(self):
        assert market.american_to_prob(100) == pytest.approx(0.5)

    def test_favorite(self):
        # -200 → 200/300 = 0.6667
        assert market.american_to_prob(-200) == pytest.approx(2 / 3)

    def test_underdog(self):
        # +150 → 100/250 = 0.4
        assert market.american_to_prob(150) == pytest.approx(0.4)

    def test_zero_and_garbage_return_none(self):
        assert market.american_to_prob(0) is None
        assert market.american_to_prob("abc") is None
        assert market.american_to_prob(None) is None


class TestDevigTwoWay:
    def test_sums_to_100(self):
        out = market.devig_two_way(-120, +100)
        assert out is not None
        assert out[0] + out[1] == pytest.approx(100.0)

    def test_removes_overround(self):
        # Both sides -110 → raw implied ~52.38% each (104.76% total).
        # De-vigged each side must be exactly 50%.
        over, under = market.devig_two_way(-110, -110)
        assert over == pytest.approx(50.0)
        assert under == pytest.approx(50.0)

    def test_favorite_over(self):
        over, under = market.devig_two_way(-200, +150)
        assert over > under
        assert over + under == pytest.approx(100.0)

    def test_missing_side_returns_none(self):
        assert market.devig_two_way(None, -110) is None
        assert market.devig_two_way(-110, 0) is None


class TestBlend:
    def test_pure_model(self):
        assert market.blend(70.0, 40.0, weight=0.0) == pytest.approx(70.0)

    def test_pure_market(self):
        assert market.blend(70.0, 40.0, weight=1.0) == pytest.approx(40.0)

    def test_halfway(self):
        assert market.blend(70.0, 40.0, weight=0.5) == pytest.approx(55.0)

    def test_clamped_to_5_95(self):
        assert market.blend(100.0, 100.0, weight=0.5) <= 95.0
        assert market.blend(0.0, 0.0, weight=0.5) >= 5.0

    def test_weight_clamped(self):
        # weight > 1 behaves like weight == 1
        assert market.blend(70.0, 40.0, weight=5.0) == pytest.approx(40.0)


class TestDynamicWeight:
    def test_more_books_more_weight(self):
        w1 = market.dynamic_weight(0.50, n_books=1)
        w2 = market.dynamic_weight(0.50, n_books=2)
        w3 = market.dynamic_weight(0.50, n_books=3)
        assert w1 < w2 < w3

    def test_single_book_discounted(self):
        assert market.dynamic_weight(0.50, n_books=1) == pytest.approx(0.30)

    def test_consensus_boosted(self):
        assert market.dynamic_weight(0.50, n_books=5) == pytest.approx(0.55)

    def test_never_exceeds_cap(self):
        assert market.dynamic_weight(1.0, n_books=10) <= 0.85


class TestTranslateProbToLine:
    def test_same_line_is_identity(self):
        import pandas as pd
        s = pd.Series([20, 22, 18, 25, 19, 21])
        out = market.translate_prob_to_line(60.0, 24.5, 24.5, "Points", 21.0, s)
        assert out == pytest.approx(60.0)

    def test_lower_target_line_raises_prob(self):
        # A lower line is easier to clear, so translated P(over) should rise.
        import pandas as pd
        s = pd.Series([20, 22, 18, 25, 19, 21, 23, 17])
        out = market.translate_prob_to_line(55.0, 22.5, 20.5, "Points", 21.0, s)
        assert out is not None
        assert out > 55.0

    def test_far_apart_returns_none(self):
        import pandas as pd
        s = pd.Series([20, 22, 18, 25, 19, 21])
        # 22.5 → 10.5 is way beyond the trust gap → skip the anchor.
        assert market.translate_prob_to_line(55.0, 22.5, 10.5, "Points", 21.0, s) is None

    def test_unfittable_series_returns_none(self):
        import pandas as pd
        s = pd.Series([20, 21])  # too short to fit a distribution
        assert market.translate_prob_to_line(55.0, 22.5, 21.5, "Points", 21.0, s) is None


class TestConsensusBuilder:
    def test_modal_line_wins(self):
        from data.odds_api_market import OddsAPIMarketClient
        groups = {
            ("luka doncic", "Points", 24.5): [55.0, 57.0, 56.0],  # 3 books
            ("luka doncic", "Points", 25.5): [48.0],              # 1 book
        }
        out = OddsAPIMarketClient._build_consensus(groups)
        entry = out[("luka doncic", "Points")]
        assert entry["line"] == 24.5
        assert entry["n_books"] == 3
        assert entry["p_over"] == pytest.approx(56.0)
        assert entry["p_under"] == pytest.approx(44.0)
