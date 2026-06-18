"""
Teammate-injury usage boost.

When a team's key player is ruled out, his minutes, shots, and touches don't
vanish — they redistribute to the teammates who remain.  Sportsbooks adjust
their lines for this quickly, but PrizePicks lines are softer and often lag,
so the players soaking up that vacated usage are a recurring source of edge.

This module turns the count of a player's sidelined teammates into a forward
looking multiplier applied to his projected mean for *counting* stats (points,
rebounds, assists, shots, etc.).  Rate-style or fixed-opportunity stats are left
untouched.  The magnitude is deliberately modest and capped — the Phase-2
backtest is the mechanism for calibrating it precisely.
"""
from __future__ import annotations

from config import (
    USAGE_BOOST_PER_TEAMMATE_OUT,
    USAGE_BOOST_DOUBTFUL_WEIGHT,
    USAGE_BOOST_MAX,
)


def weighted_out_count(out_teammates: list[tuple[str, str]]) -> float:
    """Severity-weighted count of sidelined teammates.

    "out" counts as a full absence; "doubtful" counts as a partial one (it's not
    certain they'll miss the game).  Input is a list of (name, severity) tuples
    as returned by injury_client.get_team_out_players.
    """
    total = 0.0
    for _name, severity in out_teammates:
        if severity == "out":
            total += 1.0
        elif severity == "doubtful":
            total += USAGE_BOOST_DOUBTFUL_WEIGHT
    return total


def usage_boost_multiplier(
    out_teammates: list[tuple[str, str]],
    stat_type: str,
    sport_config: dict,
) -> float:
    """Return a multiplier (>= 1.0) for a player's projected mean.

    1.0 means no boost (no sidelined teammates, or a non-counting stat).  Each
    weighted absence adds USAGE_BOOST_PER_TEAMMATE_OUT, capped at USAGE_BOOST_MAX.
    """
    counting_stats = sport_config.get("counting_stats", set())
    if stat_type not in counting_stats:
        return 1.0

    weighted = weighted_out_count(out_teammates)
    if weighted <= 0:
        return 1.0

    boost = min(USAGE_BOOST_MAX, weighted * USAGE_BOOST_PER_TEAMMATE_OUT)
    return 1.0 + boost
