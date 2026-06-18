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


def dynamic_weight(base_weight: float, n_books: int) -> float:
    """Scale the market blend weight by how many books back the consensus.

    A single book is noisy and may be soft, so it gets a fraction of the base
    weight; a multi-book consensus is the sharpest public signal available and
    earns the full (slightly boosted) weight.  Clamped to [0, 0.85] so the model
    is never fully overridden.

        n_books >= 3  → full base weight, nudged up 10%
        n_books == 2  → 85% of base
        n_books == 1  → 60% of base
    """
    if n_books >= 3:
        w = base_weight * 1.10
    elif n_books == 2:
        w = base_weight * 0.85
    else:
        w = base_weight * 0.60
    return float(max(0.0, min(0.85, w)))


def translate_prob_to_line(
    p_over_at_book_line: float,
    book_line: float,
    target_line: float,
    stat_type: str,
    projected_mean: float,
    series,
) -> float | None:
    """Translate a de-vigged P(over) priced at book_line to a different target_line.

    A de-vigged market probability is only valid for the exact line it was priced
    at.  When the sportsbook line differs from the PrizePicks line, we shift the
    probability using the player's own fitted distribution: compute the model's
    P(over) at both lines and apply the book's *additive* offset (the book's edge
    over the raw model) to the model's probability at the target line.

        market_target = model_target + (market_book − model_book)

    This preserves the book's information ("the market thinks this player is
    hotter/colder than the raw model") while moving it to the line we actually
    play.  Returns None when the distribution can't be fit (caller skips the
    blend) or when the lines are too far apart to trust the shift.
    """
    # Importing here avoids a heavy import at module load time.
    from analysis import distribution_model as _dist

    if abs(book_line - target_line) < 1e-9:
        return p_over_at_book_line

    # Only translate across a modest gap — beyond this the distribution shape
    # error swamps the market signal and we're better off skipping the anchor.
    if abs(book_line - target_line) > max(2.0, 0.25 * max(book_line, 1.0)):
        return None

    model_book = _dist.prob_over(stat_type, book_line, projected_mean, series)
    model_target = _dist.prob_over(stat_type, target_line, projected_mean, series)
    if model_book is None or model_target is None:
        return None

    offset = p_over_at_book_line - model_book[0]
    translated = model_target[0] + offset
    return float(max(1.0, min(99.0, translated)))
