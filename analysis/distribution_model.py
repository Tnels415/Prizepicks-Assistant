"""
Distribution-based P(over) for player prop lines.

- Negative Binomial for discrete counting stats with small expected value (mean < 8).
  NB handles overdispersion (variance > mean), which is the norm for real player data.
  Falls back to Poisson when the series is underdispersed.
- Gaussian for continuous stats (NFL yards) and large-expected-value counting stats
  (NBA Points, Rebounds, etc.) where the normal approximation is appropriate.

Returns None when the series is too short (<= _MIN_SERIES_LEN) to fit a distribution,
signalling the caller to fall back to the legacy empirical-hit-rate base.
"""
from __future__ import annotations

import math

import pandas as pd

# Stats that are clearly continuous even if their expected value happens to be small.
# All other stats use the mean-threshold rule: mean < 8 → NB, else Gaussian.
_CONTINUOUS_STAT_TYPES: frozenset[str] = frozenset({
    "Passing Yards", "Rushing Yards", "Receiving Yards",
    "Receiving Yards (Combo)", "Passing Yards (Combo)", "Rushing Yards (Combo)",
})

_SQRT2 = math.sqrt(2.0)
_MIN_SERIES_LEN = 5   # minimum games to estimate dispersion
_NB_THRESHOLD = 8.0   # projected_mean < this → NB; else Gaussian
_MIN_R = 0.5          # clamp NB dispersion r to avoid near-zero instability
_CDF_MAX_ITER = 500   # prevent runaway sum for large lines


def prob_over(
    stat_type: str,
    line: float,
    projected_mean: float,
    series: pd.Series,
) -> tuple[float, float] | None:
    """Return (p_over_pct, p_under_pct) in [0, 100] range, or None if unable.

    Callers should pass the full available history series (already resolved for
    combo stats via HistoricalStatsCalculator.get_stat_series).  The series is
    used only to estimate dispersion (variance); the central tendency is the
    caller-supplied projected_mean (weighted blend of recent averages).

    Returns None when:
    - projected_mean <= 0
    - fewer than _MIN_SERIES_LEN games in series
    - fitting or CDF computation fails for any reason
    """
    if projected_mean <= 0:
        return None

    series = series.dropna()
    if len(series) < _MIN_SERIES_LEN:
        return None

    dist = _choose_distribution(stat_type, projected_mean)

    try:
        if dist == "negbinom":
            p_over = _negbinom_prob_over(line, projected_mean, series)
        else:
            p_over = _gaussian_prob_over(line, projected_mean, series)
    except Exception:
        return None

    if p_over is None:
        return None

    p_over_pct = max(0.0, min(100.0, p_over * 100.0))
    return p_over_pct, 100.0 - p_over_pct


# ---------------------------------------------------------------------------
# Distribution selection
# ---------------------------------------------------------------------------

def _choose_distribution(stat_type: str, projected_mean: float) -> str:
    """Return 'negbinom' or 'gaussian' based on stat type and expected scale."""
    if stat_type in _CONTINUOUS_STAT_TYPES:
        return "gaussian"
    if projected_mean < _NB_THRESHOLD:
        return "negbinom"
    return "gaussian"


# ---------------------------------------------------------------------------
# Negative Binomial path
# ---------------------------------------------------------------------------

def _negbinom_prob_over(line: float, mean: float, series: pd.Series) -> float | None:
    """P(X > line) where X ~ NegBin or Poisson, fitted from series variance."""
    var = float(series.var())
    if var <= 0:
        return None

    # Underdispersed (var ≤ mean) → Poisson with lambda = projected_mean
    if var <= mean:
        return _poisson_prob_over(line, mean)

    # Method-of-moments NB: r = mean²/(var−mean), p = r/(r+mean)
    r = max(_MIN_R, (mean ** 2) / (var - mean))
    p = r / (r + mean)

    k_max = int(math.floor(line))
    cdf = 0.0
    for k in range(0, min(k_max + 1, _CDF_MAX_ITER)):
        cdf += _negbinom_pmf(k, r, p)
        if cdf >= 1.0 - 1e-12:
            return 0.0
    return max(0.0, 1.0 - cdf)


def _poisson_prob_over(line: float, lam: float) -> float:
    """P(X > line) where X ~ Poisson(lam)."""
    k_max = int(math.floor(line))
    cdf = 0.0
    for k in range(0, min(k_max + 1, _CDF_MAX_ITER)):
        cdf += _poisson_pmf(k, lam)
        if cdf >= 1.0 - 1e-12:
            return 0.0
    return max(0.0, 1.0 - cdf)


def _poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(k * math.log(lam) - lam - math.lgamma(k + 1))


def _negbinom_pmf(k: int, r: float, p: float) -> float:
    """NB PMF for non-integer r using log-gamma."""
    log_pmf = (
        math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1)
        + r * math.log(p)
        + k * math.log(1.0 - p)
    )
    return math.exp(log_pmf)


# ---------------------------------------------------------------------------
# Gaussian path
# ---------------------------------------------------------------------------

def _gaussian_prob_over(line: float, mean: float, series: pd.Series) -> float | None:
    """P(X > line) where X ~ Normal(mean, sigma), sigma estimated from series."""
    sigma = float(series.std())
    if sigma <= 0:
        # Degenerate — player always produces the same value
        return 1.0 if mean > line else 0.0
    z = (line - mean) / sigma
    # P(X > line) = 1 - Φ(z) = 0.5*(1 − erf(z/√2))
    return 0.5 * (1.0 - math.erf(z / _SQRT2))
