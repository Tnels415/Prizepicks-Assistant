from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from learning.history_store import HistoryStore, FACTOR_NAMES

logger = logging.getLogger(__name__)

BUCKET_SIZE = 10
MIN_BUCKET_SAMPLE = 15
MAX_CORRECTION = 20.0
MAX_WEIGHT_SHIFT = 0.30
MIN_STAT_SAMPLE = 20
MIN_FACTOR_SAMPLE = 10


@dataclass
class Corrections:
    calibration_map: dict[int, float] = field(default_factory=dict)
    stat_type_bias: dict[str, float] = field(default_factory=dict)
    factor_weights: dict[str, float] = field(default_factory=dict)
    has_sufficient_data: bool = False


class Calibrator:

    def __init__(self, store: HistoryStore, min_days: int = 7, sport: str = "NBA") -> None:
        self._store = store
        self._min_days = min_days
        self._sport = sport

    def compute_corrections(self, run_date: date) -> Corrections:
        rows = self._store.get_evaluated_predictions(self._min_days, sport=self._sport)
        if not rows:
            logger.info(
                "Calibrator: fewer than %d days of evaluated data — using raw model",
                self._min_days,
            )
            return Corrections(has_sufficient_data=False)

        logger.info("Calibrator: computing corrections from %d evaluated predictions", len(rows))

        calibration_map = self._compute_calibration(rows)
        stat_type_bias = self._compute_stat_type_bias(rows)
        current_weights = self._store.get_latest_factor_weights(sport=self._sport)
        new_weights, contributions, sample_sizes = self._compute_factor_weights(rows, current_weights)

        self._store.save_factor_weights(new_weights, contributions, sample_sizes, run_date, sport=self._sport)

        logger.info(
            "Calibrator: cal_buckets=%d  stat_biases=%d  weights=%s",
            len(calibration_map),
            len(stat_type_bias),
            {k: round(v, 3) for k, v in new_weights.items()},
        )

        return Corrections(
            calibration_map=calibration_map,
            stat_type_bias=stat_type_bias,
            factor_weights=new_weights,
            has_sufficient_data=True,
        )

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------

    def _compute_calibration(self, rows: list[dict]) -> dict[int, float]:
        buckets: dict[int, list[int]] = {}
        for row in rows:
            prob = row["hit_probability"]
            bucket = int(prob // BUCKET_SIZE) * BUCKET_SIZE
            if bucket not in buckets:
                buckets[bucket] = []
            buckets[bucket].append(row["correct"])

        result: dict[int, float] = {}
        for bucket_lo, corrects in buckets.items():
            if len(corrects) < MIN_BUCKET_SAMPLE:
                continue
            midpoint = bucket_lo + BUCKET_SIZE // 2
            predicted_rate = midpoint / 100.0
            actual_rate = sum(corrects) / len(corrects)
            correction = (actual_rate - predicted_rate) * 100.0
            result[midpoint] = max(-MAX_CORRECTION, min(MAX_CORRECTION, correction))

        return result

    # ------------------------------------------------------------------
    # Per-stat-type bias
    # ------------------------------------------------------------------

    def _compute_stat_type_bias(self, rows: list[dict]) -> dict[str, float]:
        groups: dict[str, list[dict]] = {}
        for row in rows:
            st = row["stat_type"]
            groups.setdefault(st, []).append(row)

        result: dict[str, float] = {}
        for stat_type, group in groups.items():
            if len(group) < MIN_STAT_SAMPLE:
                continue
            actual_hit_rate = sum(r["correct"] for r in group) / len(group)
            mean_predicted = sum(r["hit_probability"] for r in group) / len(group) / 100.0
            bias = (actual_hit_rate - mean_predicted) * 100.0
            result[stat_type] = max(-MAX_CORRECTION, min(MAX_CORRECTION, bias))

        return result

    # ------------------------------------------------------------------
    # Factor weight learning
    # ------------------------------------------------------------------

    def _compute_factor_weights(
        self,
        rows: list[dict],
        current_weights: dict[str, float] | None,
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
            if len(nonzero) < MIN_FACTOR_SAMPLE:
                contributions[factor] = 0.5
                sample_sizes[factor] = len(nonzero)
                continue

            correct_count = sum(
                1 for r in nonzero
                if self._factor_pointed_correctly(
                    r["raw_adjustments"][factor],
                    r["direction"],
                    r["correct"],
                )
            )
            contributions[factor] = correct_count / len(nonzero)
            sample_sizes[factor] = len(nonzero)

        # Compute target weights from accuracy
        new_weights: dict[str, float] = {}
        for factor in FACTOR_NAMES:
            acc = contributions[factor]
            target = 1.0 + 2.0 * (acc - 0.5)
            target = max(0.4, min(1.6, target))

            current = current_weights.get(factor, 1.0)
            delta = target - current
            delta = max(-MAX_WEIGHT_SHIFT, min(MAX_WEIGHT_SHIFT, delta))
            new_weights[factor] = current + delta

        # Normalize so mean weight == 1.0
        mean_w = sum(new_weights.values()) / len(new_weights)
        if mean_w > 0:
            new_weights = {f: w / mean_w for f, w in new_weights.items()}

        return new_weights, contributions, sample_sizes

    @staticmethod
    def _factor_pointed_correctly(delta: float, direction: str, correct: int) -> bool:
        if direction == "OVER":
            return (delta > 0 and correct == 1) or (delta < 0 and correct == 0)
        else:
            return (delta < 0 and correct == 1) or (delta > 0 and correct == 0)
