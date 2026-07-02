"""Tests for the accuracy overhaul: grading fixes, pick-group tracking,
Best-Bets gating, and the UNDER unlock for sportsbook-sourced props."""
from __future__ import annotations

import sys
import types
from datetime import date, timedelta

# Stub nba_api so prop_analyzer's transitive imports succeed without the package.
for _mod in [
    "nba_api", "nba_api.stats", "nba_api.stats.static",
    "nba_api.stats.static.teams", "nba_api.stats.endpoints",
    "nba_api.stats.endpoints.leaguedashplayerstats",
    "nba_api.stats.endpoints.playergamelog",
    "nba_api.stats.endpoints.leaguedashteamstats",
    "nba_api.stats.endpoints.commonplayerinfo",
]:
    sys.modules.setdefault(_mod, types.ModuleType(_mod))

import pytest  # noqa: E402

from analysis.prop_analyzer import PropResult, is_best_bet, select_best_bets  # noqa: E402
from learning.history_store import HistoryStore  # noqa: E402
from learning.outcome_fetcher import OutcomeFetcher  # noqa: E402


# ---------------------------------------------------------------------------
# Grading (outcome_fetcher._resolve_outcome)
# ---------------------------------------------------------------------------

STAT_MAP = {"Points": "PTS", "Pts+Reb": ["PTS", "REB"]}


def _pred(direction="OVER", stat="Points", line=20.5):
    return {"stat_type": stat, "line": line, "direction": direction}


class TestResolveOutcome:
    def test_normal_over_win(self):
        actual, correct = OutcomeFetcher._resolve_outcome(
            _pred(), {"played": True, "PTS": 25}, STAT_MAP)
        assert (actual, correct) == (25.0, 1)

    def test_normal_over_loss(self):
        actual, correct = OutcomeFetcher._resolve_outcome(
            _pred(), {"played": True, "PTS": 15}, STAT_MAP)
        assert (actual, correct) == (15.0, 0)

    def test_normal_under_win(self):
        actual, correct = OutcomeFetcher._resolve_outcome(
            _pred("UNDER"), {"played": True, "PTS": 15}, STAT_MAP)
        assert (actual, correct) == (15.0, 1)

    def test_combo_stat_sums(self):
        actual, correct = OutcomeFetcher._resolve_outcome(
            _pred(stat="Pts+Reb", line=30.5), {"played": True, "PTS": 25, "REB": 10}, STAT_MAP)
        assert (actual, correct) == (35.0, 1)

    def test_dnp_excluded(self):
        _, correct = OutcomeFetcher._resolve_outcome(
            _pred(), {"played": False}, STAT_MAP)
        assert correct == -1

    def test_missing_played_key_excluded_not_graded(self):
        # Regression: used to default played=True and grade against 0.
        _, correct = OutcomeFetcher._resolve_outcome(_pred(), {"PTS": 25}, STAT_MAP)
        assert correct == -1

    def test_missing_stat_column_excluded_not_zero(self):
        # Regression: missing column coerced to 0.0 → automatic OVER loss.
        _, correct = OutcomeFetcher._resolve_outcome(
            _pred(), {"played": True, "REB": 8}, STAT_MAP)
        assert correct == -1

    def test_missing_combo_component_excluded(self):
        _, correct = OutcomeFetcher._resolve_outcome(
            _pred(stat="Pts+Reb", line=30.5), {"played": True, "PTS": 25}, STAT_MAP)
        assert correct == -1

    def test_unmapped_stat_type_excluded(self):
        # Regression: fell back to stat_type[:3].upper() which never matched.
        _, correct = OutcomeFetcher._resolve_outcome(
            _pred(stat="Fantasy Score"), {"played": True, "PTS": 25}, STAT_MAP)
        assert correct == -1

    def test_zero_is_a_real_grade_when_column_present(self):
        # An explicit 0 (e.g. 0 home runs) is a legitimate actual value.
        actual, correct = OutcomeFetcher._resolve_outcome(
            _pred(line=0.5), {"played": True, "PTS": 0}, STAT_MAP)
        assert (actual, correct) == (0.0, 0)


# ---------------------------------------------------------------------------
# Pick groups (history_store)
# ---------------------------------------------------------------------------

def _result(name="Player A", stat="Points", direction="OVER", rank=1, prob=65.0):
    return PropResult(
        player_name=name, team_abbr="BOS", position="G", stat_type=stat,
        line=20.5, direction=direction, hit_probability=prob, rank=rank,
        predicted_value=25.0,
        over_probability=prob, under_probability=100.0 - prob,
        season_avg=24.0, last_5_avg=25.0, last_10_avg=24.5,
        hit_rate_20=0.65, games_analyzed=20,
        opponent_team_abbr="NYK", opponent_def_rank=15, location="Home",
        rest_days=1, h2h_hit_rate=0.0, h2h_avg=0.0, h2h_sample_size=0,
    )


@pytest.fixture
def store(tmp_path):
    return HistoryStore(db_path=tmp_path / "test_history.db")


class TestPickGroups:
    def test_migration_adds_pick_group(self, store):
        with store._conn() as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(predictions)")]
        assert "pick_group" in cols

    def test_mark_and_filter(self, store):
        d = date(2026, 6, 1)
        results = [_result(f"P{i}", rank=i + 1) for i in range(5)]
        store.save_predictions(results, d, {}, sport="MLB")
        store.mark_pick_group(d, results[:2], "emailed")
        store.mark_pick_group(d, results[:1], "best_bet")

        # Grade all as correct so accuracy queries see them.
        with store._conn() as conn:
            conn.execute("UPDATE predictions SET correct=1, actual_value=25")

        assert store.get_cumulative_accuracy()["total_evaluated"] == 5
        assert store.get_cumulative_accuracy(pick_group="emailed")["total_evaluated"] == 2
        assert store.get_cumulative_accuracy(pick_group="best_bet")["total_evaluated"] == 1

    def test_best_bet_not_downgraded_to_emailed(self, store):
        d = date(2026, 6, 1)
        results = [_result("P0")]
        store.save_predictions(results, d, {}, sport="MLB")
        store.mark_pick_group(d, results, "best_bet")
        store.mark_pick_group(d, results, "emailed")  # must not overwrite
        rows = store.get_results_for_date(d, pick_group="best_bet")
        assert len(rows) == 1

    def test_get_evaluated_since_window(self, store):
        recent, old = date.today() - timedelta(days=2), date.today() - timedelta(days=90)
        store.save_predictions([_result("New")], recent, {}, sport="MLB")
        store.save_predictions([_result("Old")], old, {}, sport="MLB")
        with store._conn() as conn:
            conn.execute("UPDATE predictions SET correct=1")
        rows = store.get_evaluated_since(days=30)
        assert [r["player_name"] for r in rows] == ["New"]


# ---------------------------------------------------------------------------
# Best Bets gate
# ---------------------------------------------------------------------------

def _bb_result(**overrides):
    """A PropResult that passes every Best-Bets gate unless overridden."""
    r = _result()
    r.tier = "A"
    r.edge = 0.11
    r.data_quality = "full"
    r.games_analyzed = 20
    r.direction = "OVER"
    r.raw_adjustments = {"_market_over": 60.0, "_injury": "none", "_std_line": True}
    for k, v in overrides.items():
        setattr(r, k, v)
    return r


class TestBestBetGate:
    def test_qualifying_pick_passes(self):
        assert is_best_bet(_bb_result()) is True

    def test_requires_a_tier(self):
        assert is_best_bet(_bb_result(tier="speculative")) is False

    def test_requires_full_data(self):
        assert is_best_bet(_bb_result(data_quality="partial")) is False

    def test_requires_min_games(self):
        assert is_best_bet(_bb_result(games_analyzed=10)) is False

    def test_requires_standard_line(self):
        r = _bb_result()
        r.raw_adjustments["_std_line"] = False
        assert is_best_bet(r) is False

    def test_requires_clean_injury(self):
        r = _bb_result()
        r.raw_adjustments["_injury"] = "questionable"
        assert is_best_bet(r) is False

    def test_requires_market_quote(self):
        r = _bb_result()
        r.raw_adjustments["_market_over"] = None
        assert is_best_bet(r) is False

    def test_requires_market_agreement_over(self):
        r = _bb_result()
        r.raw_adjustments["_market_over"] = 50.0  # market lukewarm on the over
        assert is_best_bet(r) is False

    def test_market_agreement_flips_for_under(self):
        r = _bb_result(direction="UNDER")
        r.raw_adjustments["_market_over"] = 40.0  # market P(under)=60 ≥ 55
        assert is_best_bet(r) is True

    def test_select_caps_and_dedupes(self):
        from config import BEST_BETS_MAX_PER_DAY
        picks = [_bb_result() for _ in range(BEST_BETS_MAX_PER_DAY + 3)]
        for i, p in enumerate(picks):
            p.player_name = f"P{i}"
            p.edge = 0.10 + i * 0.001
        # Duplicate player+stat with both directions — only one side may survive.
        dup_over, dup_under = _bb_result(), _bb_result(direction="UNDER")
        dup_under.raw_adjustments["_market_over"] = 40.0
        dup_over.player_name = dup_under.player_name = "Dup"
        dup_over.edge, dup_under.edge = 0.30, 0.29
        selected = select_best_bets({"MLB": picks + [dup_over, dup_under]})
        assert len(selected) <= BEST_BETS_MAX_PER_DAY
        dup_rows = [r for r in selected if r.player_name == "Dup"]
        assert len(dup_rows) == 1 and dup_rows[0].direction == "OVER"


# ---------------------------------------------------------------------------
# UNDER unlock for sportsbook-sourced props
# ---------------------------------------------------------------------------

class TestUnderUnlock:
    @staticmethod
    def _under_ok(pick_type: str, n_variants: int, prop_source: str) -> bool:
        # Mirrors the gate in _analyze_single_prop.
        is_goblin = pick_type == "goblin"
        is_demon = pick_type == "demon"
        is_two_way_book = prop_source not in ("", "PrizePicks")
        return (not is_goblin and not is_demon) and (n_variants >= 3 or is_two_way_book)

    def test_sportsbook_single_variant_allows_under(self):
        for source in ("DraftKings", "Underdog", "FanDuel", "Bovada"):
            assert self._under_ok("standard", 1, source) is True

    def test_prizepicks_single_variant_still_suppresses_under(self):
        assert self._under_ok("unknown", 1, "PrizePicks") is False

    def test_prizepicks_three_variants_allows_under(self):
        assert self._under_ok("standard", 3, "PrizePicks") is True

    def test_goblin_never_allows_under(self):
        assert self._under_ok("goblin", 3, "DraftKings") is False
