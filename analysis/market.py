"""
Market-odds utilities: convert American odds to probabilities, remove the
sportsbook vig (de-vig), and blend a devigged market probability with the
model's own probability.

The sharp sportsbook line is the single strongest public predictor of a prop
outcome.  Anchoring the model to a devigged market probability is the
highest-EV correctness improvement available (see config.MARKET_BLEND_WEIGHT).

All probabilities are handled in percentage space ([0, 100]) to match the rest
of the analysis pipeline (factor_scorer, prop_analyzer).
"""
from __future__ import annotations


def american_to_prob(odds: float) -> float | None:
    """Convert American odds to an implied probability in [0, 1] (with vig).

    Returns None for unusable input (zero / non-numeric).
    """
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return None
    if o == 0:
        return None
    if o < 0:
        return (-o) / ((-o) + 100.0)
    return 100.0 / (o + 100.0)


def devig_two_way(over_odds: float, under_odds: float) -> tuple[float, float] | None:
    """Remove vig from a two-way (Over/Under) market.

    Returns (p_over_pct, p_under_pct) summing to 100, or None if either side
    is missing/invalid.  Uses the standard multiplicative (normalization)
    method: each side's raw implied probability divided by the overround.
    """
    p_over_raw = american_to_prob(over_odds)
    p_under_raw = american_to_prob(under_odds)
    if p_over_raw is None or p_under_raw is None:
        return None
    total = p_over_raw + p_under_raw
    if total <= 0:
        return None
    p_over = p_over_raw / total
    return p_over * 100.0, (1.0 - p_over) * 100.0


def blend(model_prob_pct: float, market_prob_pct: float, weight: float) -> float:
    """Blend a model probability with a devigged market probability.

    weight is the share given to the market (0 = model only, 1 = market only).
    Result is clamped to [5, 95] to mirror the model's own clamping so a single
    extreme market number can't produce a 0%/100% pick.
    """
    w = max(0.0, min(1.0, weight))
    blended = (1.0 - w) * model_prob_pct + w * market_prob_pct
    return float(max(5.0, min(95.0, blended)))
