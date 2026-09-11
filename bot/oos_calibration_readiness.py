"""Read-only OOS calibration readiness for persisted NEXUS confidence evidence.

Uses only completed PAPER evidence rows. It never changes NEXUS confidence,
thresholds, scores, risk, sizing, release state, or exchange behavior.
"""
from __future__ import annotations

from dataclasses import asdict
from bot.nexus_probability import PROBABILITY_MODEL, heuristic_win_probability
import time

from bot.oos_model_validation import (
    ValidationRow,
    aggregate_oos_report,
    promotion_decision,
    purged_walk_forward,
)

_REPORT_INTERVAL_SECONDS = 1800.0
_last_report_ts = 0.0


def _row_values(row):
    keys = ("timestamp", "confidence", "outcome_label", "outcome_r")
    if hasattr(row, "keys"):
        return tuple(row[k] for k in keys)
    return tuple(row)


def normalize_completed_rows(rows) -> tuple[list[ValidationRow], int]:
    """Reconstruct the EV heuristic from stored confidence, excluding bad rows."""
    valid: list[ValidationRow] = []
    invalid = 0
    for raw in rows or []:
        try:
            timestamp, confidence, outcome, r_multiple = _row_values(raw)
            item = ValidationRow(
                timestamp=float(timestamp),
                confidence=heuristic_win_probability(confidence),
                outcome=int(outcome),
                r_multiple=float(r_multiple),
            ).validate()
            valid.append(item)
        except (TypeError, ValueError, OverflowError, KeyError, IndexError):
            invalid += 1
    valid.sort(key=lambda item: item.timestamp)
    return valid, invalid


async def load_completed_paper_rows(db):
    rows = await db._fetchall(
        "SELECT timestamp,confidence,outcome_label,outcome_r "
        "FROM nexus_confidence_evidence "
        "WHERE mode=? AND outcome_label IS NOT NULL AND outcome_r IS NOT NULL "
        "ORDER BY id ASC",
        ("PAPER",),
    )
    return normalize_completed_rows(rows)


def build_readiness(rows: list[ValidationRow], *, invalid_rows: int = 0) -> dict:
    """Build chronological purged walk-forward readiness without model mutation."""
    folds = purged_walk_forward(
        rows,
        train_size=60,
        test_size=20,
        purge_size=5,
        step_size=20,
    ) if len(rows) >= 85 else []

    if not folds:
        return {
            "completed": len(rows),
            "invalid": int(invalid_rows),
            "folds": 0,
            "oos_n": 0,
            "ready": False,
            "blockers": ("INSUFFICIENT_FOR_WALK_FORWARD",),
            "report": None,
            "decision_effect": "NONE",
            "execution_effect": "NONE",
        }

    rep = aggregate_oos_report(folds)
    ready, blockers = promotion_decision(rep)
    return {
        "completed": len(rows),
        "invalid": int(invalid_rows),
        "folds": len(folds),
        "oos_n": rep.n,
        "ready": bool(ready),
        "blockers": tuple(blockers),
        "report": asdict(rep),
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }


def compact_readiness_log(snapshot: dict) -> str:
    rep = snapshot.get("report") or {}
    blockers = ",".join(snapshot.get("blockers") or ()) or "NONE"

    def metric(name):
        value = rep.get(name)
        return "NA" if value is None else f"{float(value):.6f}"

    return (
        "[NEXUS_OOS_CALIBRATION] "
        f"completed={int(snapshot.get('completed', 0))} "
        f"invalid={int(snapshot.get('invalid', 0))} "
        f"folds={int(snapshot.get('folds', 0))} "
        f"oos_n={int(snapshot.get('oos_n', 0))} "
        f"ready={bool(snapshot.get('ready', False))} "
        f"brier={metric('brier')} ece={metric('ece')} "
        f"win_rate={metric('win_rate')} expectancy_r={metric('expectancy_r')} "
        f"calibration_slope={metric('calibration_slope')} blockers={blockers} "
        f"probability_model={PROBABILITY_MODEL} probability_source=reconstructed_heuristic "
        "empirically_calibrated=False decision_effect=NONE execution_effect=NONE"
    )


async def maybe_log_readiness(db, log, *, now: float | None = None, force: bool = False):
    """Emit a throttled read-only calibration report."""
    global _last_report_ts
    current = time.time() if now is None else float(now)
    if not force and _last_report_ts > 0 and current - _last_report_ts < _REPORT_INTERVAL_SECONDS:
        return None
    _last_report_ts = current

    try:
        rows, invalid = await load_completed_paper_rows(db)
        snapshot = build_readiness(rows, invalid_rows=invalid)
        log.info("%s", compact_readiness_log(snapshot))
        return snapshot
    except Exception as exc:
        log.warning(
            "[NEXUS_OOS_CALIBRATION] report_failed error=%s "
            "decision_effect=NONE execution_effect=NONE",
            type(exc).__name__,
        )
        return None
