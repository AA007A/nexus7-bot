"""Build auditable NEXUS OOS candidate datasets without changing trading runtime.

This module is analytics-only. It converts chronological replay observations into
CandidateOutcome rows consumed by nexus_oos_edge_gate. Unknown rejected outcomes
remain unknown; they are never imputed from approved trades.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Iterable

from bot.nexus_oos_edge_gate import CandidateOutcome, EdgeEvidenceReport, build_edge_report, edge_promotion_decision


@dataclass(frozen=True)
class ReplayObservation:
    timestamp: float
    approved: bool
    baseline_eligible: bool
    confidence: float
    entry: float
    stop_loss: float
    exit_price: float | None
    costs_r: float = 0.0

    def to_candidate(self) -> CandidateOutcome:
        ts = float(self.timestamp)
        conf = float(self.confidence)
        entry = float(self.entry)
        stop = float(self.stop_loss)
        costs = float(self.costs_r)
        if not all(isfinite(v) for v in (ts, conf, entry, stop, costs)):
            raise ValueError("non-finite replay observation")
        if not 0.0 <= conf <= 1.0:
            raise ValueError("confidence must be in [0,1]")
        risk = abs(entry - stop)
        if risk <= 0.0:
            raise ValueError("entry and stop_loss must define positive risk")
        if self.exit_price is None:
            return CandidateOutcome(ts, bool(self.approved), bool(self.baseline_eligible), conf, False, None)
        exit_price = float(self.exit_price)
        if not isfinite(exit_price):
            raise ValueError("exit_price must be finite")
        direction = 1.0 if stop < entry else -1.0
        gross_r = direction * (exit_price - entry) / risk
        net_r = gross_r - costs
        return CandidateOutcome(ts, bool(self.approved), bool(self.baseline_eligible), conf, True, net_r)


def build_candidate_dataset(rows: Iterable[ReplayObservation]) -> tuple[CandidateOutcome, ...]:
    """Return a deterministic chronological dataset; duplicate timestamps fail closed."""
    vals = sorted((r.to_candidate() for r in rows), key=lambda r: r.timestamp)
    seen: set[float] = set()
    for row in vals:
        if row.timestamp in seen:
            raise ValueError("duplicate candidate timestamp")
        seen.add(row.timestamp)
    return tuple(vals)


def audit_edge_dataset(rows: Iterable[ReplayObservation], **gate_kwargs) -> tuple[EdgeEvidenceReport, bool, tuple[str, ...]]:
    """Build the report and return the exact fail-closed promotion decision."""
    dataset = build_candidate_dataset(rows)
    rep = build_edge_report(dataset)
    ok, blockers = edge_promotion_decision(rep, **gate_kwargs)
    return rep, ok, blockers
