from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date

from analysis import backtest as _backtest
from learning.history_store import HistoryStore, FACTOR_NAMES

logger = logging.getLogger(__name__)

BUCKET_SIZE = 10
MAX_CORRECTION = 20.0
MAX_WEIGHT_SHIFT = 0.30

# Shrinkage constants: correction scales as n / (n + K).
# K chosen so that ~K predictions produce a half-strength correction.
_K_BUCKET = 30      # buckets need ~30 samples for full signal
_K_STAT = 40        # stat-type bias needs ~40
_K_FACTOR = 20      # factor weight needs ~20
_K_PLAYER = 20      # player bias needs ~20

# Time-decay: predictions older than HALF_LIFE_DAYS contribute half as much.
_HALF_LIFE_DAYS = 30


def _decay_weight(row: dict, today: date) -> float:
    """Exponential decay weight: 0.5 ** (age_days / HALF_LIFE_DAYS)."""
    try:
        pred_date = date.fromisoformat(row["date"])
        age = (today - pred_date).days
        return 0.5 ** (age / _HALF_LIFE_DAYS)
    except Exception:
        return 1.0


@dataclass
class Corrections:
    calibration_map: dict[int, float] = field(default_factory=dict)
    stat_type_bias: dict[str, float] = field(default_factory=dict)
    factor_weights: dict[str, float] = field(default_factory=dict)
    player_bias: dict[tuple, float] = field(default_factory=dict)
    brier_score: float | None = None
    has_sufficient_data: bool = False
    market_blend_weight: float | None = None  # auto-tuned blend weight (overrides config when set)


class Calibrator:

    def __init__(self, store: HistoryStore, min_days: int = 7, sport: str = "NBA") -> None:
        self._store = store
        self._min_days = min_days
        self._sport = sport

    def compute_corrections(self, run_date: date) -> Corrections:
        # Fetch all evaluated predictions regardless of the 7-day gate —
        # shrinkage handles small samples gracefully.
        rows = self._store.get_all_evaluated_predictions(sport=self._sport)

        # Player-level bias: always computed; shrinkage keeps it safe with few samples.
        player_bias = self._compute_player_bias(rows, run_date)

        brier = self._compute_brier_score(rows)

        n_days = self._store.count_distinct_dates(sport=self._sport)
        if not rows:
            logger.info(
                "Calibrator: no evaluated predictions yet — using raw model",
            )
            return Corrections(
                has_sufficient_data=False,
                player_bias=player_bias,
                brier_score=brier,
            )

        logger.info(
            "Calibrator: computing corrections from %d evaluated predictions "
            "(%d distinct days)  Brier=%.4f",
            len(rows), n_days, brier if brier is not None else float("nan"),
        )

        calibration_map = self._compute_calibration(rows, run_date)
        stat_type_bias = self._compute_stat_type_bias(rows, run_date)
        current_weights = self._store.get_latest_factor_weights(sport=self._sport)
        new_weights, contributions, sample_sizes = self._compute_factor_weights(
            rows, current_weights, run_date
        )

        self._store.save_factor_weights(new_weights, contributions, sample_sizes, run_date, sport=self._sport)

        market_blend_weight = self._compute_market_blend_weight(rows)

        logger.info(
            "Calibrator: cal_buckets=%d  stat_biases=%d  weights=%s  market_blend=%.3f",
            len(calibration_map),
            len(stat_type_bias),
            {k: round(v, 3) for k, v in new_weights.items()},
            market_blend_weight if market_blend_weight is not None else float("nan"),
        )

        return Corrections(
            calibration_map=calibration_map,
            stat_type_bias=stat_type_bias,
            factor_weights=new_weights,
            player_bias=player_bias,
            brier_score=brier,
            has_sufficient_data=True,
            market_blend_weight=market_blend_weight,
        )

    # ------------------------------------------------------------------
    # Calibration buckets — shrinkage + time-decay
    # ------------------------------------------------------------------

    def _compute_calibration(self, rows: list[dict], today: date) -> dict[int, float]:
        # Weighted accumulation per bucket.
        #
        # Direction consistency: the resulting delta is applied to over_prob in
        # PropAnalyzer (prop_analyzer.py, calibration_map lookup), so the map
        # must be built from OVER-direction rows only. Mixing UNDER rows (whose
        # hit_probability is 100-over) into the same buckets muddies the signal
        # with a quantity the correction is never applied to.
        rows = [r for r in rows if r.get("direction") == "OVER"]

        buckets_w_correct: dict[int, float] = {}
        buckets_w_total: dict[int, float] = {}

        for row in rows:
            prob = row["hit_probability"]
            bucket = int(prob // BUCKET_SIZE) * BUCKET_SIZE
            w = _decay_weight(row, today)
            buckets_w_correct.setdefault(bucket, 0.0)
            buckets_w_total.setdefault(bucket, 0.0)
            buckets_w_correct[bucket] += w * row["correct"]
            buckets_w_total[bucket] += w

        result: dict[int, float] = {}
        for bucket_lo, w_total in buckets_w_total.items():
            w_correct = buckets_w_correct[bucket_lo]
            midpoint = bucket_lo + BUCKET_SIZE // 2
            predicted_rate = midpoint / 100.0
            actual_rate = w_correct / w_total if w_total > 0 else predicted_rate

            raw_correction = (actual_rate - predicted_rate) * 100.0
            # Effective n from weights: use w_total as a proxy (decay-adjusted sample count).
            shrinkage = w_total / (w_total + _K_BUCKET)
            correction = raw_correction * shrinkage
            result[midpoint] = max(-MAX_CORRECTION, min(MAX_CORRECTION, correction))

        return result

    # ------------------------------------------------------------------
    # Per-stat-type bias — shrinkage + time-decay
    # ------------------------------------------------------------------

    def _compute_stat_type_bias(self, rows: list[dict], today: date) -> dict[str, float]:
        groups_w_correct: dict[str, float] = {}
        groups_w_pred: dict[str, float] = {}
        groups_w_total: dict[str, float] = {}

        for row in rows:
            st = row["stat_type"]
            w = _decay_weight(row, today)
            groups_w_correct.setdefault(st, 0.0)
            groups_w_pred.setdefault(st, 0.0)
            groups_w_total.setdefault(st, 0.0)
            groups_w_correct[st] += w * row["correct"]
            groups_w_pred[st] += w * row["hit_probability"] / 100.0
            groups_w_total[st] += w

        result: dict[str, float] = {}
        for stat_type, w_total in groups_w_total.items():
            actual_rate = groups_w_correct[stat_type] / w_total
            mean_predicted = groups_w_pred[stat_type] / w_total
            raw_bias = (actual_rate - mean_predicted) * 100.0
            shrinkage = w_total / (w_total + _K_STAT)
            result[stat_type] = max(-MAX_CORRECTION, min(MAX_CORRECTION, raw_bias * shrinkage))

        return result

    # ------------------------------------------------------------------
    # Factor weight learning — shrinkage + time-decay
    # ------------------------------------------------------------------

    def _compute_factor_weights(
        self,
        rows: list[dict],
        current_weights: dict[str, float] | None,
        today: date,
    ) -> tuple[dict[str, float], dict[str, float], dict[str, int]]:
        if current_weights is None:
            current_weights = {f: 1.0 for f in FACTOR_NAMES}

        contributions: dict[str, float] = {}
        sample_sizes: dict[str, int] = {}

        for factor in FACTOR_NAMES:
            nonzero = [
                r for r in rows
                if isinstance(r.get("raw_adjustments"), dict)
                and r["raw_adjustments"].get(factor, 0.0) != 0.0
            ]
            sample_sizes[factor] = len(nonzero)

            if not nonzero:
                contributions[factor] = 0.5
                continue

            w_correct = sum(
                _decay_weight(r, today)
                for r in nonzero
                if self._factor_pointed_correctly(
                    r["raw_adjustments"][factor],
                    r["direction"],
                    r["correct"],
                )
            )
            w_total = sum(_decay_weight(r, today) for r in nonzero)
            acc = w_correct / w_total if w_total > 0 else 0.5
            contributions[factor] = acc

        # Compute target weights with shrinkage
        new_weights: dict[str, float] = {}
        for factor in FACTOR_NAMES:
            acc = contributions[factor]
            n = sample_sizes[factor]
            shrinkage = n / (n + _K_FACTOR) if n > 0 else 0.0
            # Shrink accuracy toward 0.5 (neutral) when n is small.
            shrunk_acc = 0.5 + (acc - 0.5) * shrinkage
            target = 1.0 + 2.0 * (shrunk_acc - 0.5)
            target = max(0.4, min(1.6, target))

            current = current_weights.get(factor, 1.0)
            delta = max(-MAX_WEIGHT_SHIFT, min(MAX_WEIGHT_SHIFT, target - current))
            new_weights[factor] = current + delta

        # Normalize so mean weight == 1.0
        mean_w = sum(new_weights.values()) / len(new_weights)
        if mean_w > 0:
            new_weights = {f: w / mean_w for f, w in new_weights.items()}

        return new_weights, contributions, sample_sizes

    # ------------------------------------------------------------------
    # Player-level bias — shrinkage + time-decay
    # ------------------------------------------------------------------

    def _compute_player_bias(self, rows: list[dict], today: date) -> dict[tuple, float]:
        """Compute per-(player, stat_type) bias with shrinkage and time-decay."""
        groups_w_correct: dict[tuple, float] = {}
        groups_w_pred: dict[tuple, float] = {}
        groups_w_total: dict[tuple, float] = {}

        for row in rows:
            key = (row["player_name"], row["stat_type"])
            w = _decay_weight(row, today)
            groups_w_correct.setdefault(key, 0.0)
            groups_w_pred.setdefault(key, 0.0)
            groups_w_total.setdefault(key, 0.0)
            # Align probability to the predicted direction (OVER or UNDER).
            if row["direction"] == "OVER":
                pred = row["hit_probability"] / 100.0
            else:
                pred = 1.0 - row["hit_probability"] / 100.0
            groups_w_correct[key] += w * row["correct"]
            groups_w_pred[key] += w * pred
            groups_w_total[key] += w

        result: dict[tuple, float] = {}
        for key, w_total in groups_w_total.items():
            actual_rate = groups_w_correct[key] / w_total
            mean_predicted = groups_w_pred[key] / w_total
            raw_bias = (actual_rate - mean_predicted) * 100.0
            shrinkage = w_total / (w_total + _K_PLAYER)
            bias = raw_bias * shrinkage
            if abs(bias) > 0.5:  # suppress noise below half a percentage point
                result[key] = max(-MAX_CORRECTION, min(MAX_CORRECTION, bias))

        return result

    # ------------------------------------------------------------------
    # Market blend weight — auto-tuned from market agreement vs model accuracy
    # ------------------------------------------------------------------

    def _compute_market_blend_weight(self, rows: list[dict]) -> float | None:
        """Adjust MARKET_BLEND_WEIGHT based on whether market or model is more accurate.

        If market accuracy > model accuracy by a meaningful margin, increase the blend;
        if model is better, decrease it. Returns None when there's insufficient data.
        """
        from config import MARKET_BLEND_WEIGHT

        # Need at least 30 rows with market data for a stable signal.
        market_info = _backtest.market_agreement(rows)
        if market_info is None or market_info.get("n", 0) < 30:
            return None

        market_acc = market_info["market_accuracy"]  # 0-100

        graded = [r for r in rows if r.get("correct") in (0, 1)]
        if not graded:
            return None
        model_acc = sum(1 for r in graded if r["correct"] == 1) / len(graded) * 100.0

        gap = market_acc - model_acc  # positive → market beats model

        # Each 5pp gap moves weight by 0.05, capped at ±0.15 shift from config default.
        shift = max(-0.15, min(0.15, gap / 5.0 * 0.05))
        new_weight = max(0.20, min(0.85, MARKET_BLEND_WEIGHT + shift))

        logger.info(
            "Calibrator: market_acc=%.1f%%  model_acc=%.1f%%  gap=%.1fpp  "
            "market_blend: %.2f → %.2f",
            market_acc, model_acc, gap, MARKET_BLEND_WEIGHT, new_weight,
        )
        return new_weight

    # ------------------------------------------------------------------
    # Brier score (B3) — model calibration quality metric
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_brier_score(rows: list[dict]) -> float | None:
        """Mean squared error between predicted probability and outcome (0 or 1).

        Lower is better; a naive 50% guesser scores 0.25. Perfect calibration = 0.
        """
        if not rows:
            return None
        total = sum(
            (row["hit_probability"] / 100.0 - row["correct"]) ** 2
            for row in rows
        )
        return total / len(rows)

    @staticmethod
    def _factor_pointed_correctly(delta: float, direction: str, correct: int) -> bool:
        if direction == "OVER":
            return (delta > 0 and correct == 1) or (delta < 0 and correct == 0)
        else:
            return (delta < 0 and correct == 1) or (delta > 0 and correct == 0)
