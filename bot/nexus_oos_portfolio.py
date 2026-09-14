"""Multi-asset OOS evidence aggregation for NEXUS.

Analytics only. Portfolio inference clusters the bootstrap by decision timestamp so
simultaneous crypto candidates are resampled together instead of pretending that
cross-asset observations from the same market event are independent.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from bot.nexus_oos_edge_gate import (
    CandidateOutcome,
    EdgeEvidenceReport,
    build_edge_report,
    edge_promotion_decision,
)
from bot.nexus_oos_replay import ReplayEvidence


@dataclass(frozen=True)
class PortfolioCandidate:
    symbol: str
    outcome: CandidateOutcome

    def validate(self) -> "PortfolioCandidate":
        symbol = str(self.symbol).strip().upper()
        if not symbol:
            raise ValueError("portfolio candidate symbol is required")
        self.outcome.validate()
        return self


def _rows(replays: Iterable[ReplayEvidence]) -> tuple[PortfolioCandidate, ...]:
    vals: list[PortfolioCandidate] = []
    seen: set[tuple[str, float]] = set()
    for replay in replays:
        symbol = str(replay.symbol).strip().upper()
        if not symbol:
            raise ValueError("replay symbol is required")
        for outcome in replay.candidates:
            item = PortfolioCandidate(symbol, outcome).validate()
            key = (symbol, float(outcome.timestamp))
            if key in seen:
                raise ValueError(f"duplicate portfolio candidate: {symbol}@{outcome.timestamp}")
            seen.add(key)
            vals.append(item)
    vals.sort(key=lambda item: (item.outcome.timestamp, item.symbol))
    return tuple(vals)


def portfolio_csv(replays: Iterable[ReplayEvidence]) -> str:
    out = io.StringIO(newline="")
    fields = (
        "symbol", "timestamp", "approved", "baseline_eligible",
        "confidence", "outcome_known", "r_multiple",
    )
    writer = csv.DictWriter(out, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for item in _rows(replays):
        row = item.outcome
        writer.writerow(
            {
                "symbol": item.symbol,
                "timestamp": format(float(row.timestamp), ".17g"),
                "approved": "1" if row.approved else "0",
                "baseline_eligible": "1" if row.baseline_eligible else "0",
                "confidence": format(float(row.confidence), ".17g"),
                "outcome_known": "1" if row.outcome_known else "0",
                "r_multiple": "" if row.r_multiple is None else format(float(row.r_multiple), ".17g"),
            }
        )
    return out.getvalue()


def portfolio_sha256(replays: Iterable[ReplayEvidence]) -> str:
    return hashlib.sha256(portfolio_csv(replays).encode("utf-8")).hexdigest()


def _mean(values: list[float]) -> float:
    if not values:
        raise ValueError("empty sample")
    return sum(values) / len(values)


def _cluster_bootstrap_ci(
    rows: tuple[PortfolioCandidate, ...], *, bootstrap_samples: int, seed: int
) -> tuple[float | None, float | None]:
    known = [
        item for item in rows
        if item.outcome.baseline_eligible and item.outcome.outcome_known
    ]
    if len(known) < 2 or bootstrap_samples <= 0:
        return None, None

    clusters: dict[float, list[PortfolioCandidate]] = {}
    for item in known:
        clusters.setdefault(float(item.outcome.timestamp), []).append(item)
    keys = sorted(clusters)
    if len(keys) < 2:
        return None, None
    if sum(1 for item in known if item.outcome.approved) < 2:
        return None, None

    rng = random.Random(seed)
    diffs: list[float] = []
    for _ in range(int(bootstrap_samples)):
        sampled: list[PortfolioCandidate] = []
        for _cluster in range(len(keys)):
            sampled.extend(clusters[keys[rng.randrange(len(keys))]])
        base = [float(item.outcome.r_multiple) for item in sampled]
        approved = [
            float(item.outcome.r_multiple)
            for item in sampled if item.outcome.approved
        ]
        if not approved:
            continue
        diffs.append(_mean(approved) - _mean(base))

    if len(diffs) < max(20, int(bootstrap_samples) // 10):
        return None, None
    diffs.sort()
    lo_i = max(0, int(0.025 * len(diffs)))
    hi_i = min(len(diffs) - 1, int(0.975 * len(diffs)))
    return diffs[lo_i], diffs[hi_i]


def build_portfolio_edge_report(
    replays: Iterable[ReplayEvidence], *, bootstrap_samples: int = 4000, seed: int = 7
) -> EdgeEvidenceReport:
    items = _rows(replays)
    outcomes = [item.outcome for item in items]
    candidate_report = build_edge_report(outcomes, bootstrap_samples=0, seed=seed)
    ci_low, ci_high = _cluster_bootstrap_ci(
        items, bootstrap_samples=bootstrap_samples, seed=seed
    )
    return EdgeEvidenceReport(
        total_candidates=candidate_report.total_candidates,
        baseline_candidates=candidate_report.baseline_candidates,
        approved_candidates=candidate_report.approved_candidates,
        rejected_candidates=candidate_report.rejected_candidates,
        known_baseline_outcomes=candidate_report.known_baseline_outcomes,
        known_approved_outcomes=candidate_report.known_approved_outcomes,
        known_rejected_outcomes=candidate_report.known_rejected_outcomes,
        counterfactual_coverage=candidate_report.counterfactual_coverage,
        baseline_expectancy_r=candidate_report.baseline_expectancy_r,
        nexus_expectancy_r=candidate_report.nexus_expectancy_r,
        expectancy_uplift_r=candidate_report.expectancy_uplift_r,
        bootstrap_ci_low_r=ci_low,
        bootstrap_ci_high_r=ci_high,
    )


def write_portfolio_evidence_bundle(
    replays: Iterable[ReplayEvidence],
    output_dir: str | Path,
    *,
    bootstrap_samples: int = 4000,
    seed: int = 7,
    gate_kwargs: dict | None = None,
) -> dict:
    vals = tuple(replays)
    csv_text = portfolio_csv(vals)
    digest = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
    report = build_portfolio_edge_report(
        vals, bootstrap_samples=bootstrap_samples, seed=seed
    )
    proven, blockers = edge_promotion_decision(report, **dict(gate_kwargs or {}))

    per_symbol = {}
    for replay in sorted(vals, key=lambda replay: replay.symbol):
        per_symbol[str(replay.symbol).upper()] = {
            "parity": replay.parity,
            "baseline_trade_count": replay.baseline_trade_count,
            "evaluated_count": replay.evaluated_count,
            "warmup_excluded_count": replay.warmup_excluded_count,
            "report": asdict(build_edge_report(replay.candidates, bootstrap_samples=bootstrap_samples, seed=seed)),
        }

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "nexus_oos_portfolio_candidates.csv").write_text(csv_text, encoding="utf-8")
    payload = {
        "schema_version": 1,
        "status": "AI_EDGE_PROVEN" if proven else "AI_EDGE_NOT_PROVEN",
        "proven": proven,
        "blockers": list(blockers),
        "dataset_sha256": digest,
        "bootstrap_samples": int(bootstrap_samples),
        "bootstrap_seed": int(seed),
        "bootstrap_unit": "decision_timestamp_cluster",
        "parity": "CORE_CANDLES_ONLY",
        "portfolio_report": asdict(report),
        "per_symbol": per_symbol,
    }
    (destination / "nexus_oos_portfolio_report.json").write_text(
        json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return payload
