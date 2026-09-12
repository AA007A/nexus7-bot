"""Durable, observational storage for session-penalty counterfactual cohorts.

This module persists only shadow evidence. It never authorizes execution, changes
strategy/risk thresholds, modifies leverage, sizes positions, or calls exchange
order routes. Database failures are best-effort observability failures only.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from typing import Any

from bot import database as db

_TABLE_READY = False
_TABLE_LOCK = asyncio.Lock()
_MAX_RESTORE_AGE_S = 6 * 60 * 60


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _json_safe(state: dict) -> dict:
    """Return a JSON-safe copy without changing the caller's state."""
    safe = {}
    for key, value in dict(state or {}).items():
        if value is None or isinstance(value, (str, bool, int)):
            safe[str(key)] = value
        elif isinstance(value, float):
            safe[str(key)] = value if math.isfinite(value) else 0.0
        else:
            safe[str(key)] = str(value)
    return safe


async def ensure_table(log) -> bool:
    """Create the dedicated shadow table lazily; never fail trading startup."""
    global _TABLE_READY
    if _TABLE_READY:
        return True
    async with _TABLE_LOCK:
        if _TABLE_READY:
            return True
        sql = """CREATE TABLE IF NOT EXISTS session_penalty_shadow_audit (
            state_key TEXT PRIMARY KEY,
            created_epoch REAL,
            updated_epoch REAL,
            status TEXT,
            symbol TEXT,
            direction TEXT,
            market_session TEXT,
            penalty INTEGER,
            base_score REAL,
            adjusted_score REAL,
            min_score REAL,
            nexus_status TEXT,
            nexus_approved INTEGER,
            outcome TEXT,
            payload TEXT
        )"""
        ok = await db._exec(sql)
        if not ok:
            return False
        await db._exec(
            "CREATE INDEX IF NOT EXISTS idx_session_shadow_status "
            "ON session_penalty_shadow_audit(status)"
        )
        await db._exec(
            "CREATE INDEX IF NOT EXISTS idx_session_shadow_updated "
            "ON session_penalty_shadow_audit(updated_epoch)"
        )
        _TABLE_READY = True
        log.info(
            "[SESSION_PENALTY_PERSISTENCE] table_ready=true restore_window_h=6 "
            "trading_effect=NONE execution_effect=NONE"
        )
        return True


async def save_state(state_key: str, state: dict, *, status: str = "OPEN", log) -> bool:
    """Idempotently persist one cohort snapshot."""
    if not state_key or not await ensure_table(log):
        return False
    now = time.time()
    snapshot = _json_safe(state)
    created = _finite(snapshot.get("created_epoch"), now)
    snapshot["created_epoch"] = created
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    approved = snapshot.get("nexus_approved")
    approved_db = None if approved is None else (1 if approved is True else 0)
    sql = """INSERT INTO session_penalty_shadow_audit (
        state_key,created_epoch,updated_epoch,status,symbol,direction,
        market_session,penalty,base_score,adjusted_score,min_score,
        nexus_status,nexus_approved,outcome,payload
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(state_key) DO UPDATE SET
        updated_epoch=excluded.updated_epoch,
        status=excluded.status,
        symbol=excluded.symbol,
        direction=excluded.direction,
        market_session=excluded.market_session,
        penalty=excluded.penalty,
        base_score=excluded.base_score,
        adjusted_score=excluded.adjusted_score,
        min_score=excluded.min_score,
        nexus_status=excluded.nexus_status,
        nexus_approved=excluded.nexus_approved,
        outcome=excluded.outcome,
        payload=excluded.payload"""
    params = (
        state_key,
        created,
        now,
        str(status or "OPEN"),
        str(snapshot.get("symbol", "")),
        str(snapshot.get("direction", "")),
        str(snapshot.get("session", "")),
        int(_finite(snapshot.get("penalty"), 0)),
        _finite(snapshot.get("base_score")),
        _finite(snapshot.get("adjusted_score")),
        _finite(snapshot.get("min_score")),
        str(snapshot.get("nexus_status", "NOT_CHECKED")),
        approved_db,
        str(snapshot.get("outcome", "")),
        payload,
    )
    ok = await db._exec(sql, params)
    if not ok:
        log.debug(
            "[SESSION_PENALTY_PERSISTENCE] save_failed key=%s "
            "trading_effect=NONE execution_effect=NONE",
            state_key,
        )
    return bool(ok)


async def load_open(log, *, max_age_s: float = _MAX_RESTORE_AGE_S):
    """Return unresolved recent cohorts, or None when persistence is unavailable."""
    if not await ensure_table(log):
        return None
    cutoff = time.time() - max(60.0, float(max_age_s))
    rows = await db._fetchall(
        """SELECT state_key,payload FROM session_penalty_shadow_audit
           WHERE status='OPEN' AND updated_epoch>=?
           ORDER BY updated_epoch ASC LIMIT 250""",
        (cutoff,),
    )
    restored = []
    for row in rows or []:
        try:
            key = str(row[0])
            state = json.loads(row[1]) if row[1] else {}
            if not isinstance(state, dict):
                continue
            # A restart between enrollment and the final NEXUS answer can leave
            # either NOT_CHECKED or IN_PROGRESS durable. Both are explicitly
            # non-authorizing after restart; forward price-path tracking resumes.
            if state.get("nexus_status") in {"NOT_CHECKED", "IN_PROGRESS"}:
                state["nexus_status"] = "INTERRUPTED_RESTART"
                state["nexus_approved"] = False
                state["nexus_reason"] = "process_restart_before_counterfactual_completed"
            restored.append((key, state))
        except Exception as exc:
            log.debug(
                "[SESSION_PENALTY_PERSISTENCE] restore_row_failed error=%s "
                "trading_effect=NONE execution_effect=NONE",
                type(exc).__name__,
            )
    return restored
