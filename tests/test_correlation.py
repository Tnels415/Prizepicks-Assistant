"""Tests for analysis.correlation — pairwise correlation, joint probability, entries."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from analysis import correlation as corr


@dataclass
class Leg:
    player_name: str
    stat_type: str
    direction: str
    hit_probability: float
    team_abbr: str = "BOS"
    opponent_team_abbr: str = "MIA"
    line: float = 10.0
    sport: str = "NBA"
    tier: str = "A"
    edge: float = 0.1


class TestPairwiseCorrelation:
    def test_same_player_related_stats_positive(self):
        a = Leg("Tatum", "Points", "OVER", 60)
        b = Leg("Tatum", "Pts+Reb+Ast", "OVER", 60)
        assert corr.pairwise_correlation(a, b) > 0.4

    def test_same_player_opposite_direction_negative(self):
        a = Leg("Tatum", "Points", "OVER", 60)
        b = Leg("Tatum", "Assists", "UNDER", 60)
        assert corr.pairwise_correlation(a, b) < 0

    def test_different_games_uncorrelated(self):
        a = Leg("Tatum", "Points", "OVER", 60, team_abbr="BOS", opponent_team_abbr="MIA")
        b = Leg("Doncic", "Points", "OVER", 60, team_abbr="DAL", opponent_team_abbr="PHX")
        assert corr.pairwise_correlation(a, b) == 0.0

    def test_opponents_same_game_positive(self):
        a = Leg("Tatum", "Points", "OVER", 60, team_abbr="BOS", opponent_team_abbr="MIA")
        b = Leg("Butler", "Points", "OVER", 60, team_abbr="MIA", opponent_team_abbr="BOS")
        assert corr.pairwise_correlation(a, b) > 0

    def test_cross_sport_zero(self):
        a = Leg("Tatum", "Points", "OVER", 60, sport="NBA")
        b = Leg("Ohtani", "Hits", "OVER", 60, sport="MLB")
        assert corr.pairwise_correlation(a, b) == 0.0


class TestJointHitProbability:
    def test_independent_is_product(self):
        legs = [
            Leg("A", "Points", "OVER", 60, team_abbr="BOS", opponent_team_abbr="MIA"),
            Leg("B", "Points", "OVER", 50, team_abbr="DAL", opponent_team_abbr="PHX"),
        ]
        # Different games → rho≈0 → joint ≈ 0.6 * 0.5 = 0.30
        assert corr.joint_hit_probability(legs) == pytest.approx(0.30, abs=0.01)

    def test_positive_correlation_beats_independence(self):
        legs = [
            Leg("Tatum", "Points", "OVER", 60),
            Leg("Tatum", "Pts+Reb+Ast", "OVER", 60),
        ]
        indep = 0.6 * 0.6
        assert corr.joint_hit_probability(legs) > indep

    def test_single_leg_is_marginal(self):
        legs = [Leg("A", "Points", "OVER", 65)]
        assert corr.joint_hit_probability(legs) == pytest.approx(0.65)

    def test_bounded(self):
        legs = [Leg("A", "Points", "OVER", 99), Leg("B", "Points", "OVER", 99,
                team_abbr="DAL", opponent_team_abbr="PHX")]
        p = corr.joint_hit_probability(legs)
        assert 0.0 <= p <= 1.0


class TestBuildEntry:
    def test_no_duplicate_player_stat(self):
        cands = [
            Leg("A", "Points", "OVER", 70),
            Leg("A", "Points", "OVER", 68),   # conflict (same player+stat)
            Leg("B", "Points", "OVER", 65, team_abbr="DAL", opponent_team_abbr="PHX"),
        ]
        entry = corr.build_entry(cands, size=2, play_type="power")
        assert entry is not None
        names_stats = {(l.player_name, l.stat_type) for l in entry.legs}
        assert len(names_stats) == 2

    def test_returns_none_when_too_few(self):
        cands = [Leg("A", "Points", "OVER", 70)]
        assert corr.build_entry(cands, size=3) is None

    def test_power_ev_computed(self):
        cands = [
            Leg("A", "Points", "OVER", 70, team_abbr="BOS", opponent_team_abbr="MIA"),
            Leg("B", "Points", "OVER", 70, team_abbr="DAL", opponent_team_abbr="PHX"),
        ]
        entry = corr.build_entry(cands, size=2, play_type="power", payout_multiplier=3.0)
        assert entry.expected_value is not None


class TestRecommendPowerEntries:
    def test_filters_negative_ev(self):
        # Two coin-flip legs in a 3x power play: 0.5*0.5*3 - 1 = -0.25 → filtered
        cands = [
            Leg("A", "Points", "OVER", 50, team_abbr="BOS", opponent_team_abbr="MIA"),
            Leg("B", "Points", "OVER", 50, team_abbr="DAL", opponent_team_abbr="PHX"),
        ]
        out = corr.recommend_power_entries(cands, (2,), {2: 3.0}, min_ev=0.0)
        assert out == []

    def test_keeps_positive_ev(self):
        cands = [
            Leg("A", "Points", "OVER", 80, team_abbr="BOS", opponent_team_abbr="MIA"),
            Leg("B", "Points", "OVER", 80, team_abbr="DAL", opponent_team_abbr="PHX"),
        ]
        # 0.8*0.8*3 - 1 = 0.92 → kept
        out = corr.recommend_power_entries(cands, (2,), {2: 3.0}, min_ev=0.0)
        assert len(out) == 1
        assert out[0].expected_value > 0
