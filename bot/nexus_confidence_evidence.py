"""Durable observational evidence for NEXUS confidence calibration.

This module records decision telemetry only. It never changes a NEXUS decision,
never grants execution authority and never calls exchange mutation endpoints.
"""
from __future__ import annotations

import math
import time

_TABLE_SQL = """CREATE TABLE IF NOT EXISTS nexus_confidence_evidence (
    id SERIAL PRIMARY KEY,
    timestamp TEXT,
    symbol TEXT,
    proposed_side TEXT,
    decision TEXT,
    approved INTEGER,
    confidence REAL,
    setup_quality REAL,
    data_quality REAL,
    expected_value REAL,
    risk_reward REAL,
    entry REAL,
    stop_loss REAL,
    take_profit REAL,
    market_regime TEXT,
    setup_grade TEXT,
    decision_source TEXT,
    mode TEXT,
    outcome_r REAL,
    outcome_label INTEGER
)"""

_TABLE_SQLITE = _TABLE_SQL.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")


def normalize_evidence(*, nx_dec, proposed_side: str, decision_source: str, mode: str) -> dict:
    """Normalize one NEXUS decision into calibration-safe scalar evidence."""
    if nx_dec is None:
        raise ValueError("nx_dec is required")

    def finite(name: str) -> float:
        value = getattr(nx_dec, name, None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"invalid numeric field: {name}")
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(f"non-finite field: {name}")
        return value

    confidence = finite("confidence")
    setup_quality = finite("setup_quality")
    data_quality = finite("data_quality")
    if not 0.0 <= confidence <= 100.0:
        raise ValueError("confidence outside 0..100")
    if not 0.0 <= setup_quality <= 100.0 or not 0.0 <= data_quality <= 100.0:
        raise ValueError("quality outside 0..100")

    return {
        "timestamp": str(int(time.time())),
        "symbol": str(getattr(nx_dec, "symbol", "")),
        "proposed_side": str(proposed_side),
        "decision": str(getattr(nx_dec, "decision", "")),
        "approved": 1 if getattr(nx_dec, "execution_allowed", False) is True else 0,
        "confidence": confidence,
        "setup_quality": setup_quality,
        "data_quality": data_quality,
        "expected_value": finite("expected_value"),
        "risk_reward": finite("risk_reward"),
        "entry": finite("entry"),
        "stop_loss": finite("stop_loss"),
        "take_profit": finite("take_profit"),
        "market_regime": str(getattr(nx_dec, "market_regime", "")),
        "setup_grade": str(getattr(nx_dec, "setup_grade", "")),
        "decision_source": str(decision_source),
        "mode": str(mode),
    }


async def persist_evidence(db, row: dict) -> bool:
    """Best-effort telemetry persistence; never changes execution state."""
    if not isinstance(row, dict):
        raise ValueError("row must be dict")
    try:
        ddl = _TABLE_SQL if getattr(db, "_is_pg", False) else _TABLE_SQLITE
        await db._exec(ddl)
        sql = """INSERT INTO nexus_confidence_evidence
            (timestamp,symbol,proposed_side,decision,approved,confidence,
             setup_quality,data_quality,expected_value,risk_reward,entry,stop_loss,
             take_profit,market_regime,setup_grade,decision_source,mode)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        params = tuple(row[k] for k in (
            "timestamp", "symbol", "proposed_side", "decision", "approved",
            "confidence", "setup_quality", "data_quality", "expected_value",
            "risk_reward", "entry", "stop_loss", "take_profit", "market_regime",
            "setup_grade", "decision_source", "mode",
        ))
        return bool(await db._exec(sql, params))
    except Exception:
        return False


async def capture_decision(db, *, nx_dec, proposed_side: str, decision_source: str, mode: str) -> bool:
    row = normalize_evidence(
        nx_dec=nx_dec,
        proposed_side=proposed_side,
        decision_source=decision_source,
        mode=mode,
    )
    return await persist_evidence(db, row)
