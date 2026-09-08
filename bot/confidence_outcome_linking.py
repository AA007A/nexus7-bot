"""Deterministic linkage between NEXUS confidence evidence and realized outcomes.

This module is observational only. It never changes a NEXUS decision and never
calls exchange endpoints. It links a persisted approved decision to a later
persisted trade and records the realized R multiple when that trade closes.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Mapping, Any


@dataclass(frozen=True)
class EvidenceMatch:
    evidence_id: int
    age_seconds: float


def _float(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric value")
    out = float(value)
    if not math.isfinite(out):
        raise ValueError("non-finite numeric value")
    return out


def _close(a: float, b: float, rel_tol: float = 1e-9, abs_tol: float = 1e-12) -> bool:
    return math.isclose(_float(a), _float(b), rel_tol=rel_tol, abs_tol=abs_tol)


def select_evidence_for_trade(
    rows: Iterable[Mapping[str, Any]],
    *,
    symbol: str,
    side: str,
    entry: float,
    stop_loss: float,
    opened_ts: float,
    mode: str,
    max_age_seconds: float = 900.0,
) -> EvidenceMatch | None:
    """Select the newest unlinked approved evidence that exactly describes a trade.

    Matching is deliberately strict: same symbol, side, mode, entry and stop;
    evidence must precede the trade open and be at most ``max_age_seconds`` old.
    This avoids retrospective fuzzy matching that could contaminate calibration.
    """
    opened = _float(opened_ts)
    max_age = _float(max_age_seconds)
    if max_age <= 0:
        raise ValueError("max_age_seconds must be positive")

    candidates: list[tuple[float, int]] = []
    for row in rows:
        try:
            if int(row.get("approved", 0)) != 1:
                continue
            if row.get("trade_id") not in (None, "", 0, "0"):
                continue
            if str(row.get("symbol", "")) != str(symbol):
                continue
            if str(row.get("proposed_side", "")).upper() != str(side).upper():
                continue
            if str(row.get("mode", "")).upper() != str(mode).upper():
                continue
            if not _close(row.get("entry"), entry):
                continue
            if not _close(row.get("stop_loss"), stop_loss):
                continue
            ts = _float(row.get("timestamp"))
            age = opened - ts
            if age < 0 or age > max_age:
                continue
            evidence_id = int(row.get("id"))
            if evidence_id <= 0:
                continue
            candidates.append((ts, evidence_id))
        except (TypeError, ValueError, OverflowError):
            continue

    if not candidates:
        return None
    ts, evidence_id = max(candidates, key=lambda item: item[0])
    return EvidenceMatch(evidence_id=evidence_id, age_seconds=opened - ts)


def outcome_label_from_r(r_multiple: float) -> int:
    """Binary target for win-probability calibration: positive R is a win."""
    value = _float(r_multiple)
    return 1 if value > 0.0 else 0


async def ensure_outcome_columns(db) -> None:
    """Best-effort additive schema migration for previously created evidence tables.

    The database adapter already converts DDL failures (for example duplicate
    columns on an idempotent restart) into a False return value and logs the
    error. Keeping that behavior here avoids a silent broad exception while
    preserving restart-safe, observational migration semantics.
    """
    statements = (
        "ALTER TABLE nexus_confidence_evidence ADD COLUMN trade_id INTEGER",
        "ALTER TABLE nexus_confidence_evidence ADD COLUMN linked_at TEXT",
        "ALTER TABLE nexus_confidence_evidence ADD COLUMN outcome_at TEXT",
    )
    for sql in statements:
        await db._exec(sql)


async def link_trade(db, *, trade_id: int, symbol: str, side: str, entry: float,
                     stop_loss: float, opened_ts: float, mode: str,
                     max_age_seconds: float = 900.0) -> int | None:
    """Link one persisted trade to one prior approved evidence row, or return None."""
    if int(trade_id) <= 0:
        raise ValueError("trade_id must be positive")
    await ensure_outcome_columns(db)
    rows = await db._fetchall(
        "SELECT id,timestamp,symbol,proposed_side,approved,entry,stop_loss,mode,trade_id "
        "FROM nexus_confidence_evidence WHERE symbol=? AND approved=1 "
        "ORDER BY id DESC LIMIT 50",
        (symbol,),
    )
    normalized = []
    for row in rows or []:
        if hasattr(row, "keys"):
            normalized.append({k: row[k] for k in row.keys()})
        else:
            keys = ("id","timestamp","symbol","proposed_side","approved","entry","stop_loss","mode","trade_id")
            normalized.append(dict(zip(keys, row)))
    match = select_evidence_for_trade(
        normalized,
        symbol=symbol,
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        opened_ts=opened_ts,
        mode=mode,
        max_age_seconds=max_age_seconds,
    )
    if match is None:
        return None
    ok = await db._exec(
        "UPDATE nexus_confidence_evidence SET trade_id=?,linked_at=? "
        "WHERE id=? AND trade_id IS NULL",
        (int(trade_id), str(int(opened_ts)), int(match.evidence_id)),
    )
    return int(match.evidence_id) if ok else None


async def record_trade_outcome(db, *, trade_id: int, r_multiple: float, outcome_ts: float) -> bool:
    """Record realized R and binary outcome for an already linked trade."""
    if int(trade_id) <= 0:
        raise ValueError("trade_id must be positive")
    value = _float(r_multiple)
    await ensure_outcome_columns(db)
    return bool(await db._exec(
        "UPDATE nexus_confidence_evidence SET outcome_r=?,outcome_label=?,outcome_at=? "
        "WHERE trade_id=? AND outcome_r IS NULL",
        (value, outcome_label_from_r(value), str(int(_float(outcome_ts))), int(trade_id)),
    ))
