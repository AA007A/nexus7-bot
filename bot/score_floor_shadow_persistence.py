"""Durable observational storage for score-floor near-miss cohorts.

This table contains research telemetry only. It cannot authorize execution,
change score/R:R/EV/volume thresholds, alter leverage, size positions, or touch
exchange order state. Persistence failures are non-authorizing and best-effort.
"""
from __future__ import annotations

import asyncio
import json
import math
import time

from bot import database as db

_TABLE_READY = False
_TABLE_LOCK = asyncio.Lock()
_MAX_RESTORE_AGE_S = 6 * 60 * 60


def _finite(value, default=0.0):
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _safe(state: dict) -> dict:
    out = {}
    for key, value in dict(state or {}).items():
        if value is None or isinstance(value, (str, bool, int)):
            out[str(key)] = value
        elif isinstance(value, float):
            out[str(key)] = value if math.isfinite(value) else 0.0
        elif isinstance(value, (list, tuple)):
            out[str(key)] = [v for v in value if isinstance(v, (str, bool, int, float))]
        else:
            out[str(key)] = str(value)
    return out


async def ensure_table(log) -> bool:
    global _TABLE_READY
    if _TABLE_READY:
        return True
    async with _TABLE_LOCK:
        if _TABLE_READY:
            return True
        ok = await db._exec("""CREATE TABLE IF NOT EXISTS score_floor_shadow_audit (
            state_key TEXT PRIMARY KEY,
            created_epoch REAL,
            updated_epoch REAL,
            status TEXT,
            symbol TEXT,
            direction TEXT,
            canonical_score INTEGER,
            production_floor INTEGER,
            nexus_status TEXT,
            nexus_approved INTEGER,
            outcome TEXT,
            payload TEXT
        )""")
        if not ok:
            return False
        await db._exec(
            "CREATE INDEX IF NOT EXISTS idx_score_floor_shadow_status "
            "ON score_floor_shadow_audit(status)"
        )
        await db._exec(
            "CREATE INDEX IF NOT EXISTS idx_score_floor_shadow_updated "
            "ON score_floor_shadow_audit(updated_epoch)"
        )
        _TABLE_READY = True
        log.info(
            "[SCORE_FLOOR_SHADOW_PERSISTENCE] table_ready=true restore_window_h=6 "
            "production_floor_unchanged=true execution_effect=NONE"
        )
        return True


async def save_state(key: str, state: dict, *, status: str, log) -> bool:
    if not key or not await ensure_table(log):
        return False
    now = time.time()
    snapshot = _safe(state)
    created = _finite(snapshot.get("created_epoch"), now)
    snapshot["created_epoch"] = created
    approved = snapshot.get("nexus_approved")
    approved_db = None if approved is None else (1 if approved is True else 0)
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return bool(await db._exec(
        """INSERT INTO score_floor_shadow_audit (
            state_key,created_epoch,updated_epoch,status,symbol,direction,
            canonical_score,production_floor,nexus_status,nexus_approved,outcome,payload
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(state_key) DO UPDATE SET
            updated_epoch=excluded.updated_epoch,
            status=excluded.status,
            symbol=excluded.symbol,
            direction=excluded.direction,
            canonical_score=excluded.canonical_score,
            production_floor=excluded.production_floor,
            nexus_status=excluded.nexus_status,
            nexus_approved=excluded.nexus_approved,
            outcome=excluded.outcome,
            payload=excluded.payload""",
        (
            key, created, now, str(status), str(snapshot.get("symbol", "")),
            str(snapshot.get("direction", "")), int(_finite(snapshot.get("score"), 0)),
            int(_finite(snapshot.get("production_floor"), 60)),
            str(snapshot.get("nexus_status", "NOT_CHECKED")), approved_db,
            str(snapshot.get("outcome", "")), payload,
        ),
    ))


async def load_open(log, *, max_age_s: float = _MAX_RESTORE_AGE_S):
    if not await ensure_table(log):
        return None
    cutoff = time.time() - max(60.0, float(max_age_s))
    rows = await db._fetchall(
        """SELECT state_key,payload FROM score_floor_shadow_audit
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
            if state.get("nexus_status") in {"NOT_CHECKED", "IN_PROGRESS"}:
                state["nexus_status"] = "INTERRUPTED_RESTART"
                state["nexus_approved"] = False
                state["nexus_reason"] = "process_restart_before_counterfactual_completed"
            restored.append((key, state))
        except Exception as exc:
            log.debug(
                "[SCORE_FLOOR_SHADOW_PERSISTENCE] restore_row_failed error=%s "
                "execution_effect=NONE",
                type(exc).__name__,
            )
    return restored
