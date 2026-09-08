"""Out-of-sample calibration helpers for NEXUS confidence and EV.

No training occurs on the validation slice. These helpers deliberately keep the
implementation simple and auditable so the bot can report empirical win rates,
Brier score and expectancy by score bucket before any adaptive calibration is
trusted.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Sequence


@dataclass(frozen=True)
class CalibrationRow:
    confidence: float  # 0..100
    won: bool
    r_multiple: float


@dataclass(frozen=True)
class CalibrationBucket:
    lower: float
    upper: float
    n: int
    mean_confidence: float
    empirical_win_rate: float
    expectancy_r: float


@dataclass(frozen=True)
class CalibrationReport:
    n: int
    brier_score: float
    buckets: tuple[CalibrationBucket, ...]


def calibration_report(rows: Sequence[CalibrationRow], bucket_width: int = 10) -> CalibrationReport:
    if bucket_width <= 0 or 100 % bucket_width != 0:
        raise ValueError("bucket_width must divide 100")
    clean: list[CalibrationRow] = []
    for row in rows:
        c = float(row.confidence)
        r = float(row.r_multiple)
        if not math.isfinite(c) or not math.isfinite(r) or not 0 <= c <= 100:
            raise ValueError("invalid calibration row")
        clean.append(row)
    if not clean:
        return CalibrationReport(0, 0.0, ())

    brier = sum(((row.confidence / 100.0) - (1.0 if row.won else 0.0)) ** 2 for row in clean) / len(clean)
    buckets: list[CalibrationBucket] = []
    for lower in range(0, 100, bucket_width):
        upper = lower + bucket_width
        selected = [
            row for row in clean
            if lower <= row.confidence < upper or (upper == 100 and row.confidence == 100)
        ]
        if not selected:
            continue
        buckets.append(CalibrationBucket(
            lower=float(lower),
            upper=float(upper),
            n=len(selected),
            mean_confidence=sum(r.confidence for r in selected) / len(selected),
            empirical_win_rate=sum(1 for r in selected if r.won) / len(selected),
            expectancy_r=sum(r.r_multiple for r in selected) / len(selected),
        ))
    return CalibrationReport(len(clean), brier, tuple(buckets))


def purged_walk_forward_splits(
    n_samples: int,
    *,
    train_size: int,
    test_size: int,
    purge_size: int = 0,
    step: int | None = None,
) -> list[tuple[range, range]]:
    """Create chronological train/test splits with a purge gap.

    The test window is strictly after the training window; no look-ahead and no
    random shuffling are allowed. ``purge_size`` removes observations adjacent
    to the boundary to reduce label overlap leakage.
    """
    if min(n_samples, train_size, test_size) <= 0 or purge_size < 0:
        raise ValueError("invalid walk-forward sizes")
    step = test_size if step is None else step
    if step <= 0:
        raise ValueError("step must be positive")
    out: list[tuple[range, range]] = []
    train_start = 0
    while True:
        train_end = train_start + train_size
        test_start = train_end + purge_size
        test_end = test_start + test_size
        if test_end > n_samples:
            break
        out.append((range(train_start, train_end), range(test_start, test_end)))
        train_start += step
    return out


def out_of_sample_expectancy(rows: Sequence[CalibrationRow], test_indices: Iterable[int]) -> float:
    idx = list(test_indices)
    if not idx:
        raise ValueError("empty OOS slice")
    selected = [rows[i] for i in idx]
    return sum(float(r.r_multiple) for r in selected) / len(selected)
