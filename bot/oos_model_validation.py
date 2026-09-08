"""Out-of-sample model validation primitives for NEXUS-7.

Pure analytics only: no exchange access, no order routing and no runtime
threshold mutation. The goal is to measure whether NEXUS confidence and
expectancy survive chronological, purged out-of-sample validation.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Iterable, Sequence


@dataclass(frozen=True)
class ValidationRow:
    timestamp: float
    confidence: float  # probability-like value in [0, 1]
    outcome: int       # 1 win, 0 non-win
    r_multiple: float

    def validate(self) -> "ValidationRow":
        if not isfinite(self.timestamp):
            raise ValueError("timestamp must be finite")
        if not isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0,1]")
        if self.outcome not in (0, 1):
            raise ValueError("outcome must be 0 or 1")
        if not isfinite(self.r_multiple):
            raise ValueError("r_multiple must be finite")
        return self


@dataclass(frozen=True)
class WalkForwardFold:
    train: tuple[ValidationRow, ...]
    test: tuple[ValidationRow, ...]


@dataclass(frozen=True)
class CalibrationReport:
    n: int
    brier: float
    ece: float
    win_rate: float
    expectancy_r: float
    calibration_slope: float


def purged_walk_forward(
    rows: Sequence[ValidationRow],
    *,
    train_size: int,
    test_size: int,
    purge_size: int = 0,
    step_size: int | None = None,
) -> list[WalkForwardFold]:
    """Chronological walk-forward splits with an embargo/purge gap.

    The purge removes observations immediately before each test window from the
    train slice, reducing leakage when labels depend on overlapping future
    returns. Rows are sorted by timestamp and never shuffled.
    """
    if train_size <= 0 or test_size <= 0 or purge_size < 0:
        raise ValueError("invalid walk-forward sizes")
    step = test_size if step_size is None else step_size
    if step <= 0:
        raise ValueError("step_size must be positive")

    ordered = tuple(sorted((r.validate() for r in rows), key=lambda r: r.timestamp))
    folds: list[WalkForwardFold] = []
    test_start = train_size + purge_size
    while test_start + test_size <= len(ordered):
        train_end = test_start - purge_size
        train_start = max(0, train_end - train_size)
        train = ordered[train_start:train_end]
        test = ordered[test_start:test_start + test_size]
        if len(train) == train_size and len(test) == test_size:
            folds.append(WalkForwardFold(train=train, test=test))
        test_start += step
    return folds


def brier_score(rows: Iterable[ValidationRow]) -> float:
    vals = [r.validate() for r in rows]
    if not vals:
        raise ValueError("empty validation sample")
    return sum((r.confidence - r.outcome) ** 2 for r in vals) / len(vals)


def expected_calibration_error(rows: Iterable[ValidationRow], bins: int = 10) -> float:
    vals = [r.validate() for r in rows]
    if not vals:
        raise ValueError("empty validation sample")
    if bins <= 0:
        raise ValueError("bins must be positive")
    total = len(vals)
    err = 0.0
    for i in range(bins):
        lo = i / bins
        hi = (i + 1) / bins
        bucket = [r for r in vals if (lo <= r.confidence < hi) or (i == bins - 1 and r.confidence == 1.0)]
        if not bucket:
            continue
        avg_conf = sum(r.confidence for r in bucket) / len(bucket)
        avg_outcome = sum(r.outcome for r in bucket) / len(bucket)
        err += (len(bucket) / total) * abs(avg_conf - avg_outcome)
    return err


def calibration_slope(rows: Iterable[ValidationRow]) -> float:
    """Simple OLS slope of outcome on confidence; 1 is ideal, 0 uninformative."""
    vals = [r.validate() for r in rows]
    if len(vals) < 2:
        return 0.0
    mx = sum(r.confidence for r in vals) / len(vals)
    my = sum(r.outcome for r in vals) / len(vals)
    var = sum((r.confidence - mx) ** 2 for r in vals)
    if var <= 0:
        return 0.0
    cov = sum((r.confidence - mx) * (r.outcome - my) for r in vals)
    return cov / var


def report(rows: Iterable[ValidationRow], bins: int = 10) -> CalibrationReport:
    vals = [r.validate() for r in rows]
    if not vals:
        raise ValueError("empty validation sample")
    return CalibrationReport(
        n=len(vals),
        brier=brier_score(vals),
        ece=expected_calibration_error(vals, bins=bins),
        win_rate=sum(r.outcome for r in vals) / len(vals),
        expectancy_r=sum(r.r_multiple for r in vals) / len(vals),
        calibration_slope=calibration_slope(vals),
    )


def aggregate_oos_report(folds: Sequence[WalkForwardFold], bins: int = 10) -> CalibrationReport:
    test_rows = [row for fold in folds for row in fold.test]
    if not test_rows:
        raise ValueError("no out-of-sample rows")
    return report(test_rows, bins=bins)


def promotion_decision(
    rep: CalibrationReport,
    *,
    min_samples: int = 100,
    max_brier: float = 0.24,
    max_ece: float = 0.10,
    min_expectancy_r: float = 0.0,
    min_slope: float = 0.20,
) -> tuple[bool, tuple[str, ...]]:
    """Fail-closed evidence gate for promoting a calibrated confidence model.

    This function does not mutate runtime configuration. It only returns an
    auditable decision and blockers.
    """
    blockers: list[str] = []
    if rep.n < min_samples:
        blockers.append("INSUFFICIENT_OOS_SAMPLE")
    if rep.brier > max_brier:
        blockers.append("BRIER_TOO_HIGH")
    if rep.ece > max_ece:
        blockers.append("CALIBRATION_ERROR_TOO_HIGH")
    if rep.expectancy_r <= min_expectancy_r:
        blockers.append("NON_POSITIVE_EXPECTANCY")
    if rep.calibration_slope < min_slope:
        blockers.append("CONFIDENCE_NOT_INFORMATIVE")
    return (not blockers, tuple(blockers))
