"""
Correlation-aware PrizePicks entry construction.

PrizePicks pays on *entries* (parlays), not single legs, so the realized hit
rate of an entry depends on how its legs move together — not just each leg's
standalone probability:

  - Power plays (all legs must hit) love POSITIVE correlation: legs that tend to
    hit together raise the joint probability above the independence baseline.
  - Flex plays (partial payouts) prefer DIVERSIFICATION: low/negative correlation
    spreads risk so a single blow-up doesn't sink the whole entry.

This module estimates pairwise correlation from a small set of well-understood
prop relationships, computes a correlation-adjusted joint "all hit" probability
via an equicorrelation Gaussian copula, and greedily assembles the best entry of
a given size for a given play type.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

_EPS = 1e-9


# ---------------------------------------------------------------------------
# Normal CDF / inverse-CDF (no scipy dependency)
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Inverse standard-normal CDF via Acklam's rational approximation."""
    p = min(1.0 - _EPS, max(_EPS, p))
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


# ---------------------------------------------------------------------------
# Pairwise correlation heuristics
# ---------------------------------------------------------------------------

def _game_key(leg) -> frozenset:
    return frozenset({
        str(getattr(leg, "team_abbr", "")).upper(),
        str(getattr(leg, "opponent_team_abbr", "")).upper(),
    })


# Stat groups that share an underlying driver (so two overs move together).
_RELATED_STATS = {
    "Points", "Pts+Reb+Ast", "Pts+Ast", "Pts+Reb", "3-PT Made",
    "FG Made", "FG Attempted",
}


def pairwise_correlation(a, b) -> float:
    """Heuristic correlation in [-1, 1] between two prop legs.

    Built from a few robust relationships; deliberately conservative so a wrong
    sign can't dominate.  Direction-aware: two OVERs that share a driver are
    positively correlated; an OVER paired with an UNDER on the same driver flips.
    """
    if getattr(a, "sport", None) != getattr(b, "sport", None):
        return 0.0

    same_player = getattr(a, "player_name", "") == getattr(b, "player_name", "")
    same_game = _game_key(a) == _game_key(b) and len(_game_key(a)) == 2
    same_team = str(getattr(a, "team_abbr", "")).upper() == str(getattr(b, "team_abbr", "")).upper()

    dir_a = getattr(a, "direction", "OVER")
    dir_b = getattr(b, "direction", "OVER")
    same_dir = dir_a == dir_b
    sign = 1.0 if same_dir else -1.0

    if same_player:
        # Same player's stats move strongly together on a good night.
        base = 0.55 if (a.stat_type in _RELATED_STATS and b.stat_type in _RELATED_STATS) else 0.35
        return sign * base

    if same_game and not same_team:
        # Opponents in a high-pace / high-total game: both sides' overs rise.
        return sign * 0.20

    if same_game and same_team:
        # Teammates: shared game flow (+) lightly offset by shot competition.
        return sign * 0.12

    return 0.0


def average_pairwise_correlation(legs: list) -> float:
    """Mean correlation across all unique leg pairs (0 for < 2 legs)."""
    n = len(legs)
    if n < 2:
        return 0.0
    total = 0.0
    count = 0
    for i in range(n):
        for j in range(i + 1, n):
            total += pairwise_correlation(legs[i], legs[j])
            count += 1
    return total / count if count else 0.0


# ---------------------------------------------------------------------------
# Joint "all hit" probability — equicorrelation Gaussian copula
# ---------------------------------------------------------------------------

def joint_hit_probability(legs: list, avg_corr: float | None = None) -> float:
    """P(all legs hit) under an equicorrelation Gaussian copula.

    Each leg's marginal P(hit) is hit_probability/100.  The legs share a single
    average correlation rho; the joint probability is the 1-D integral

        P(all) = ∫ φ(z) ∏_i Φ( (x_i + √rho · z) / √(1−rho) ) dz,   x_i = Φ⁻¹(p_i)

    which interpolates between independence (rho=0 → ∏ p_i) and perfect
    dependence (rho→1 → min p_i).  rho is clamped to keep the implied matrix
    positive-semidefinite for the given number of legs.
    """
    probs = [max(_EPS, min(1.0 - _EPS, lg.hit_probability / 100.0)) for lg in legs]
    n = len(probs)
    if n == 0:
        return 0.0
    if n == 1:
        return probs[0]

    rho = average_pairwise_correlation(legs) if avg_corr is None else avg_corr
    # Equicorrelation matrix is PSD only for rho >= -1/(n-1); clamp with margin.
    rho = max(-1.0 / (n - 1) + 1e-3, min(0.95, rho))

    if abs(rho) < 1e-4:
        out = 1.0
        for p in probs:
            out *= p
        return out

    xs = [_norm_ppf(p) for p in probs]
    sqrt_rho = math.sqrt(abs(rho))
    sqrt_1mrho = math.sqrt(1.0 - rho) if rho < 1.0 else _EPS

    # Trapezoidal integration of the standard-normal mixing variable.
    lo, hi, steps = -6.0, 6.0, 240
    h = (hi - lo) / steps
    total = 0.0
    for k in range(steps + 1):
        z = lo + k * h
        phi = math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
        prod = 1.0
        for x in xs:
            # For negative rho, sqrt(rho) is imaginary; the common-factor model
            # only holds for rho>=0, so we use -z for the (clamped, mildly
            # negative) case which still yields a monotone, sensible adjustment.
            shift = sqrt_rho * z if rho >= 0 else -sqrt_rho * z
            prod *= _norm_cdf((x + shift) / sqrt_1mrho)
        weight = 0.5 if (k == 0 or k == steps) else 1.0
        total += weight * phi * prod
    return float(max(0.0, min(1.0, total * h)))


# ---------------------------------------------------------------------------
# Entry construction
# ---------------------------------------------------------------------------

@dataclass
class Entry:
    legs: list
    play_type: str          # "power" or "flex"
    joint_probability: float  # P(all hit), correlation-adjusted, 0-1
    avg_correlation: float
    expected_value: float | None = None  # EV per $1 if a payout was provided


def _conflicts(leg, chosen: list) -> bool:
    """Disallow two legs on the same (player, stat) — PrizePicks forbids it and
    they're trivially, perfectly correlated."""
    for c in chosen:
        if (c.player_name == leg.player_name
                and c.stat_type == leg.stat_type):
            return True
    return False


def build_entry(
    candidates: list,
    size: int,
    play_type: str = "power",
    payout_multiplier: float | None = None,
) -> Entry | None:
    """Greedily assemble the best entry of `size` legs from ranked candidates.

    candidates should be pre-sorted best-first (highest edge).  Selection is
    correlation-aware per play type:
      - "power": prefer legs that *raise* the joint all-hit probability (reward
        positive correlation with already-chosen legs).
      - "flex":  prefer legs that *lower* average correlation (diversify), so a
        single miss is survivable.
    Returns None if fewer than `size` non-conflicting candidates exist.
    """
    if size < 1 or len(candidates) < size:
        return None

    chosen: list = [candidates[0]]
    pool = candidates[1:]

    while len(chosen) < size and pool:
        best_idx = None
        best_score = None
        for idx, leg in enumerate(pool):
            if _conflicts(leg, chosen):
                continue
            trial = chosen + [leg]
            joint = joint_hit_probability(trial)
            corr = average_pairwise_correlation(trial)
            # Power: maximize joint hit prob. Flex: maximize joint while
            # penalizing correlation (diversification preference).
            if play_type == "flex":
                score = joint - 0.5 * max(0.0, corr)
            else:
                score = joint
            if best_score is None or score > best_score:
                best_score = score
                best_idx = idx
        if best_idx is None:
            break
        chosen.append(pool.pop(best_idx))

    if len(chosen) < size:
        return None

    joint = joint_hit_probability(chosen)
    corr = average_pairwise_correlation(chosen)
    ev = None
    if payout_multiplier is not None and play_type == "power":
        # Power play pays multiplier × stake iff all legs hit, else 0.
        ev = joint * payout_multiplier - 1.0
    return Entry(
        legs=chosen,
        play_type=play_type,
        joint_probability=joint,
        avg_correlation=corr,
        expected_value=ev,
    )


def recommend_power_entries(
    candidates: list,
    sizes,
    payouts: dict[int, float],
    min_ev: float = 0.0,
) -> list[Entry]:
    """Build one correlation-aware power-play entry per requested size.

    Returns only entries whose expected value clears `min_ev` (default break-even),
    sorted by EV descending.  `candidates` should be ranked best-first.
    """
    entries: list[Entry] = []
    for size in sizes:
        payout = payouts.get(size)
        if payout is None:
            continue
        entry = build_entry(candidates, size, play_type="power", payout_multiplier=payout)
        if entry is not None and entry.expected_value is not None and entry.expected_value >= min_ev:
            entries.append(entry)
    entries.sort(key=lambda e: (e.expected_value or -1.0), reverse=True)
    return entries
