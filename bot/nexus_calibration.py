"""Offline/passive calibration metrics for NEXUS heuristic probabilities.

These helpers measure whether the versioned heuristic probability corresponds
to observed outcomes. They never modify trading decisions, thresholds, sizing,
leverage, exchange routing, or persisted trading state.
"""
from __future__ import annotations

from math import isfinite


def _validated(samples):
    out = []
    for probability, outcome in samples:
        p = float(probability)
        y = float(outcome)
        if not isfinite(p) or not 0.0 <= p <= 1.0:
            raise ValueError("probability must be finite and within 0..1")
        if y not in (0.0, 1.0):
            raise ValueError("outcome must be 0 or 1")
        out.append((p, y))
    return out


def brier_score(samples) -> float | None:
    rows = _validated(samples)
    if not rows:
        return None
    return sum((p - y) ** 2 for p, y in rows) / len(rows)


def reliability_bins(samples, bins: int = 10):
    rows = _validated(samples)
    if bins < 2:
        raise ValueError("bins must be >= 2")
    buckets = [[] for _ in range(bins)]
    for p, y in rows:
        idx = min(bins - 1, int(p * bins))
        buckets[idx].append((p, y))
    result = []
    for idx, bucket in enumerate(buckets):
        if not bucket:
            continue
        result.append({
            "bin": idx,
            "count": len(bucket),
            "mean_probability": sum(p for p, _ in bucket) / len(bucket),
            "observed_win_rate": sum(y for _, y in bucket) / len(bucket),
        })
    return result


def expected_calibration_error(samples, bins: int = 10) -> float | None:
    rows = _validated(samples)
    if not rows:
        return None
    total = len(rows)
    return sum(
        (bucket["count"] / total)
        * abs(bucket["observed_win_rate"] - bucket["mean_probability"])
        for bucket in reliability_bins(rows, bins=bins)
    )


def calibration_report(samples, bins: int = 10):
    rows = _validated(samples)
    return {
        "sample_size": len(rows),
        "brier_score": brier_score(rows),
        "ece": expected_calibration_error(rows, bins=bins),
        "reliability": reliability_bins(rows, bins=bins),
        "interpretation": "heuristic_not_empirically_calibrated_until_validated_on_oos_outcomes",
        "execution_effect": "NONE",
    }
