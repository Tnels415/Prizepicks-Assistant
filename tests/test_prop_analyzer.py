"""Regression tests for PropAnalyzer._build_key_factors.

Guards against the bug where None-valued diagnostic keys (_dist_over,
_market_over) in raw_adjustments crashed the abs()-based sort, zeroing out
every prop whenever the market-odds blend had no DraftKings match.
"""
from __future__ import annotations

import sys
import types

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

from analysis.prop_analyzer import PropAnalyzer  # noqa: E402
from config import SPORT_CONFIG  # noqa: E402


def _analyzer():
    return PropAnalyzer.__new__(PropAnalyzer)  # bypass __init__ — only need the method


def test_build_key_factors_ignores_none_diagnostics():
    a = _analyzer()
    raw_adj = {
        "season_avg_vs_line": 6.9,
        "recent_form": 5.3,
        "consistency": -4.2,
        "_dist_over": None,      # diagnostic, missing
        "_market_over": None,    # diagnostic, missing (DraftKings 403)
    }
    avgs = {"last_10_avg": 28.1}
    h2h = {"reliable": False, "sample_size": 0, "hit_rate": 0.0}
    # Must not raise, and must surface the real factors.
    out = a._build_key_factors(raw_adj, avgs, h2h, line=27.5, opponent_abbr="SAS", opp_rank=None)
    assert any("Season Avg Vs Line" in f for f in out)
    assert all("Dist Over" not in f and "Market Over" not in f for f in out)


def test_build_key_factors_includes_market_when_present():
    a = _analyzer()
    raw_adj = {
        "season_avg_vs_line": 1.0,
        "_dist_over": 60.0,
        "_market_over": 58.0,
    }
    avgs = {"last_10_avg": 20.0}
    h2h = {"reliable": False, "sample_size": 0, "hit_rate": 0.0}
    out = a._build_key_factors(raw_adj, avgs, h2h, line=19.5, opponent_abbr="SAS", opp_rank=None)
    # Underscore diagnostics are never shown as key factors, even when populated.
    assert all(not f.startswith("Dist Over") and not f.startswith("Market Over") for f in out)
