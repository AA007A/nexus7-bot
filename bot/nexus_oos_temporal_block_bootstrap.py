"""Temporal block-bootstrap inference for NEXUS OOS edge research.

Research-only. Candidate-level bootstrap preserves approved⊂baseline covariance,
but adjacent market observations can still be serially dependent and multiple
symbols can share the same market regime. This module therefore resamples
contiguous UTC time buckets, carrying every candidate in each selected bucket.
That preserves within-bucket cross-candidate dependence and short-horizon regime
clustering without changing any live trading policy.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from math import isfinite
import random
from typing import Iterable

from bot.nexus_oos_edge_gate import CandidateOutcome


@dataclass(frozen=True)
class TemporalBlockBootstrapReport:
    available: bool
    bucket_seconds: int
    block_buckets: int
    unique_buckets: int
    known_baseline_outcomes: int
    known_approved_outcomes: int
    bootstrap_samples_requested: int
    bootstrap_samples_used: int
    expectancy_uplift_r: float | None
    ci_low_r: float | None
    ci_high_r: float | None
    ci_strictly_positive: bool
    execution_effect: str = "NONE"
    promotion_authority: bool = False


def _timestamp_seconds(value: float) -> float:
    ts = float(value)
    if not isfinite(ts):
        raise ValueError("timestamp must be finite")
    # Production/replay candidates currently use epoch milliseconds. Tests and
    # future adapters may use epoch seconds; normalize without mutating source rows.
    return ts / 1000.0 if abs(ts) >= 100_000_000_000 else ts


def _mean(values: list[float]) -> float:
    if not values:
        raise ValueError("empty sample")
    return sum(values) / len(values)


def temporal_block_bootstrap(
    rows: Iterable[CandidateOutcome],
    *,
    bucket_seconds: int = 86_400,
    block_buckets: int = 3,
    bootstrap_samples: int = 4_000,
    seed: int = 29,
    min_unique_buckets: int = 8,
) -> TemporalBlockBootstrapReport:
    """Bootstrap uplift by contiguous time blocks rather than independent rows.

    The default uses UTC-day buckets and circular three-day blocks. All known
    baseline candidates sharing a bucket travel together, so contemporaneous
    cross-symbol observations are not spuriously treated as independent.
    """
    bucket_seconds = int(bucket_seconds)
    block_buckets = int(block_buckets)
    bootstrap_samples = int(bootstrap_samples)
    min_unique_buckets = int(min_unique_buckets)
    if bucket_seconds <= 0 or block_buckets <= 0 or bootstrap_samples <= 0:
        raise ValueError("bootstrap parameters must be positive")
    if min_unique_buckets < 2:
        raise ValueError("min_unique_buckets must be >=2")

    known = [
        row.validate()
        for row in rows
        if row.baseline_eligible and row.outcome_known
    ]
    approved_n = sum(1 for row in known if row.approved)

    grouped: dict[int, list[CandidateOutcome]] = {}
    for row in known:
        bucket = int(_timestamp_seconds(row.timestamp) // bucket_seconds)
        grouped.setdefault(bucket, []).append(row)
    keys = sorted(grouped)
    n_buckets = len(keys)

    base_r = [float(row.r_multiple) for row in known]
    approved_r = [float(row.r_multiple) for row in known if row.approved]
    uplift = (_mean(approved_r) - _mean(base_r)) if base_r and approved_r else None

    if (
        n_buckets < min_unique_buckets
        or len(known) < 2
        or approved_n < 2
        or block_buckets > n_buckets
    ):
        return TemporalBlockBootstrapReport(
            available=False,
            bucket_seconds=bucket_seconds,
            block_buckets=block_buckets,
            unique_buckets=n_buckets,
            known_baseline_outcomes=len(known),
            known_approved_outcomes=approved_n,
            bootstrap_samples_requested=bootstrap_samples,
            bootstrap_samples_used=0,
            expectancy_uplift_r=uplift,
            ci_low_r=None,
            ci_high_r=None,
            ci_strictly_positive=False,
        )

    rng = random.Random(seed)
    diffs: list[float] = []
    blocks_needed = (n_buckets + block_buckets - 1) // block_buckets

    for _ in range(bootstrap_samples):
        selected_keys: list[int] = []
        for _block in range(blocks_needed):
            start = rng.randrange(n_buckets)
            for offset in range(block_buckets):
                selected_keys.append(keys[(start + offset) % n_buckets])
        selected_keys = selected_keys[:n_buckets]

        sample: list[CandidateOutcome] = []
        for key in selected_keys:
            sample.extend(grouped[key])
        sample_base = [float(row.r_multiple) for row in sample]
        sample_approved = [float(row.r_multiple) for row in sample if row.approved]
        if not sample_base or not sample_approved:
            continue
        diffs.append(_mean(sample_approved) - _mean(sample_base))

    min_usable = max(20, bootstrap_samples // 10)
    if len(diffs) < min_usable:
        return TemporalBlockBootstrapReport(
            available=False,
            bucket_seconds=bucket_seconds,
            block_buckets=block_buckets,
            unique_buckets=n_buckets,
            known_baseline_outcomes=len(known),
            known_approved_outcomes=approved_n,
            bootstrap_samples_requested=bootstrap_samples,
            bootstrap_samples_used=len(diffs),
            expectancy_uplift_r=uplift,
            ci_low_r=None,
            ci_high_r=None,
            ci_strictly_positive=False,
        )

    diffs.sort()
    lo = diffs[max(0, int(0.025 * len(diffs)))]
    hi = diffs[min(len(diffs) - 1, int(0.975 * len(diffs)))]
    return TemporalBlockBootstrapReport(
        available=True,
        bucket_seconds=bucket_seconds,
        block_buckets=block_buckets,
        unique_buckets=n_buckets,
        known_baseline_outcomes=len(known),
        known_approved_outcomes=approved_n,
        bootstrap_samples_requested=bootstrap_samples,
        bootstrap_samples_used=len(diffs),
        expectancy_uplift_r=uplift,
        ci_low_r=lo,
        ci_high_r=hi,
        ci_strictly_positive=lo > 0.0,
    )


def temporal_block_bootstrap_dict(rows: Iterable[CandidateOutcome], **kwargs) -> dict:
    return asdict(temporal_block_bootstrap(rows, **kwargs))
