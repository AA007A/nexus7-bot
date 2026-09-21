"""Fail-closed statistical evidence gate for NEXUS incremental edge.

Analytics only. This module never mutates runtime thresholds, risk, leverage,
position sizing, release authorization or exchange state. Re-running its OOS
workflow is evidence refresh only and carries no promotion authority by itself.

The key methodological rule is that realized outcomes from *approved trades only*
cannot prove incremental AI edge. A baseline-vs-NEXUS comparison requires an
OOS candidate set with outcomes for both approved and rejected candidates (for
example, produced by a leakage-safe historical replay/backtest).

Bootstrap inference preserves the data-generating relationship: NEXUS-approved
candidates are a subset of the baseline candidate population. Therefore each
bootstrap draw resamples complete baseline candidates and recomputes both the
baseline and approved-subset expectancy from that same draw. Treating those two
samples as independent would discard their covariance and can misstate the
uncertainty of the uplift estimate.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
import random
from typing import Iterable


@dataclass(frozen=True)
class CandidateOutcome:
    timestamp: float
    approved: bool
    baseline_eligible: bool
    confidence: float
    outcome_known: bool
    r_multiple: float | None

    def validate(self) -> "CandidateOutcome":
        if not isfinite(float(self.timestamp)):
            raise ValueError("timestamp must be finite")
        if not isfinite(float(self.confidence)) or not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence must be in [0,1]")
        if self.outcome_known:
            if self.r_multiple is None or not isfinite(float(self.r_multiple)):
                raise ValueError("known outcome requires finite r_multiple")
        elif self.r_multiple is not None:
            raise ValueError("unknown outcome must not carry r_multiple")
        return self


@dataclass(frozen=True)
class EdgeEvidenceReport:
    total_candidates: int
    baseline_candidates: int
    approved_candidates: int
    rejected_candidates: int
    known_baseline_outcomes: int
    known_approved_outcomes: int
    known_rejected_outcomes: int
    counterfactual_coverage: float
    baseline_expectancy_r: float | None
    nexus_expectancy_r: float | None
    expectancy_uplift_r: float | None
    bootstrap_ci_low_r: float | None
    bootstrap_ci_high_r: float | None


def _mean(values: list[float]) -> float:
    if not values:
        raise ValueError("empty sample")
    return sum(values) / len(values)


def _paired_candidate_bootstrap_ci(
    known_base: list[CandidateOutcome],
    *,
    bootstrap_samples: int,
    seed: int,
) -> tuple[float | None, float | None]:
    """Bootstrap uplift while preserving approved⊂baseline dependence.

    Every draw samples complete baseline candidates. Baseline expectancy is the
    mean R across that draw; NEXUS expectancy is the mean R among approved rows
    in the *same* draw. This retains the covariance induced by selection.
    """
    if len(known_base) < 2 or bootstrap_samples <= 0:
        return None, None
    approved_total = sum(1 for row in known_base if row.approved)
    if approved_total < 2:
        return None, None

    rng = random.Random(seed)
    diffs: list[float] = []
    n = len(known_base)
    for _ in range(int(bootstrap_samples)):
        sample = [known_base[rng.randrange(n)] for _ in range(n)]
        base_r = [float(row.r_multiple) for row in sample]
        approved_r = [float(row.r_multiple) for row in sample if row.approved]
        # Vanishing approved subset is possible only in very small/highly
        # imbalanced samples. Such a bootstrap replicate carries no uplift
        # estimate and is conservatively discarded.
        if not approved_r:
            continue
        diffs.append(_mean(approved_r) - _mean(base_r))

    if len(diffs) < max(20, int(bootstrap_samples) // 10):
        return None, None
    diffs.sort()
    lo_i = max(0, int(0.025 * len(diffs)))
    hi_i = min(len(diffs) - 1, int(0.975 * len(diffs)))
    return diffs[lo_i], diffs[hi_i]


def build_edge_report(
    rows: Iterable[CandidateOutcome],
    *,
    bootstrap_samples: int = 4000,
    seed: int = 7,
) -> EdgeEvidenceReport:
    vals = [r.validate() for r in rows]
    baseline = [r for r in vals if r.baseline_eligible]
    approved = [r for r in baseline if r.approved]
    rejected = [r for r in baseline if not r.approved]
    known_base = [r for r in baseline if r.outcome_known]
    known_approved = [r for r in approved if r.outcome_known]
    known_rejected = [r for r in rejected if r.outcome_known]

    coverage = (len(known_base) / len(baseline)) if baseline else 0.0
    base_exp = _mean([float(r.r_multiple) for r in known_base]) if known_base else None
    nexus_exp = _mean([float(r.r_multiple) for r in known_approved]) if known_approved else None
    uplift = (nexus_exp - base_exp) if nexus_exp is not None and base_exp is not None else None

    ci_low, ci_high = _paired_candidate_bootstrap_ci(
        known_base,
        bootstrap_samples=bootstrap_samples,
        seed=seed,
    )

    return EdgeEvidenceReport(
        total_candidates=len(vals),
        baseline_candidates=len(baseline),
        approved_candidates=len(approved),
        rejected_candidates=len(rejected),
        known_baseline_outcomes=len(known_base),
        known_approved_outcomes=len(known_approved),
        known_rejected_outcomes=len(known_rejected),
        counterfactual_coverage=coverage,
        baseline_expectancy_r=base_exp,
        nexus_expectancy_r=nexus_exp,
        expectancy_uplift_r=uplift,
        bootstrap_ci_low_r=ci_low,
        bootstrap_ci_high_r=ci_high,
    )


def edge_promotion_decision(
    rep: EdgeEvidenceReport,
    *,
    min_baseline_samples: int = 200,
    min_approved_samples: int = 75,
    min_rejected_samples: int = 75,
    min_counterfactual_coverage: float = 0.95,
    min_expectancy_uplift_r: float = 0.0,
) -> tuple[bool, tuple[str, ...]]:
    """Return PROVEN only when incremental edge is statistically defensible."""
    blockers: list[str] = []
    if rep.known_baseline_outcomes < min_baseline_samples:
        blockers.append("INSUFFICIENT_BASELINE_OOS_SAMPLE")
    if rep.known_approved_outcomes < min_approved_samples:
        blockers.append("INSUFFICIENT_APPROVED_OOS_SAMPLE")
    if rep.known_rejected_outcomes < min_rejected_samples:
        blockers.append("INSUFFICIENT_REJECTED_COUNTERFACTUAL_SAMPLE")
    if rep.counterfactual_coverage < min_counterfactual_coverage:
        blockers.append("COUNTERFACTUAL_COVERAGE_TOO_LOW")
    if rep.expectancy_uplift_r is None or rep.expectancy_uplift_r <= min_expectancy_uplift_r:
        blockers.append("NO_POSITIVE_EXPECTANCY_UPLIFT")
    if rep.bootstrap_ci_low_r is None or rep.bootstrap_ci_low_r <= 0.0:
        blockers.append("UPLIFT_NOT_STATISTICALLY_POSITIVE")
    return (not blockers, tuple(blockers))


def evidence_status(rep: EdgeEvidenceReport, **kwargs) -> str:
    ok, _ = edge_promotion_decision(rep, **kwargs)
    return "AI_EDGE_PROVEN" if ok else "AI_EDGE_NOT_PROVEN"
