"""Deterministic, analytics-only IO for NEXUS OOS edge evidence.

This module deliberately has no runtime trading imports. It serializes complete
baseline candidate outcomes so approved and rejected observations can be audited
outside the live execution path. It never changes thresholds, leverage, sizing,
risk policy, release authorization or exchange state.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from bot.nexus_oos_edge_gate import CandidateOutcome, build_edge_report, edge_promotion_decision

_FIELDS = (
    "timestamp",
    "approved",
    "baseline_eligible",
    "confidence",
    "outcome_known",
    "r_multiple",
)


def _ordered(rows: Iterable[CandidateOutcome]) -> tuple[CandidateOutcome, ...]:
    vals = tuple(sorted((row.validate() for row in rows), key=lambda row: row.timestamp))
    seen: set[float] = set()
    for row in vals:
        if row.timestamp in seen:
            raise ValueError("duplicate candidate timestamp")
        seen.add(row.timestamp)
    return vals


def dataset_csv(rows: Iterable[CandidateOutcome]) -> str:
    """Return canonical chronological CSV suitable for hashing/review."""
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in _ordered(rows):
        writer.writerow(
            {
                "timestamp": format(float(row.timestamp), ".17g"),
                "approved": "1" if row.approved else "0",
                "baseline_eligible": "1" if row.baseline_eligible else "0",
                "confidence": format(float(row.confidence), ".17g"),
                "outcome_known": "1" if row.outcome_known else "0",
                "r_multiple": "" if row.r_multiple is None else format(float(row.r_multiple), ".17g"),
            }
        )
    return out.getvalue()


def dataset_sha256(rows: Iterable[CandidateOutcome]) -> str:
    return hashlib.sha256(dataset_csv(rows).encode("utf-8")).hexdigest()


def write_evidence_bundle(
    rows: Iterable[CandidateOutcome],
    output_dir: str | Path,
    *,
    bootstrap_samples: int = 4000,
    seed: int = 7,
    gate_kwargs: dict | None = None,
) -> dict:
    """Write dataset + decision report atomically enough for offline evidence use."""
    vals = _ordered(rows)
    csv_text = dataset_csv(vals)
    digest = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
    report = build_edge_report(vals, bootstrap_samples=bootstrap_samples, seed=seed)
    proven, blockers = edge_promotion_decision(report, **dict(gate_kwargs or {}))

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    dataset_path = destination / "nexus_oos_candidates.csv"
    report_path = destination / "nexus_oos_edge_report.json"
    dataset_path.write_text(csv_text, encoding="utf-8")

    payload = {
        "schema_version": 1,
        "status": "AI_EDGE_PROVEN" if proven else "AI_EDGE_NOT_PROVEN",
        "proven": proven,
        "blockers": list(blockers),
        "dataset_sha256": digest,
        "bootstrap_samples": int(bootstrap_samples),
        "bootstrap_seed": int(seed),
        "report": asdict(report),
    }
    report_path.write_text(
        json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return payload
