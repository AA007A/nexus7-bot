"""Persistent evidence and calibration readiness for HTF transition shadow samples.

This module is observability only. It cannot authorize or submit trades. It
persists the shadow cohort created by ``htf_transition_shadow`` and computes a
conservative evidence gate for a future human-reviewed production change.

A transition policy is marked REVIEW_READY only when resolved, non-ambiguous
samples satisfy all of these conditions:
- at least 40 resolved samples;
- positive mean net expectancy after the existing cost model;
- positive one-sided 95% lower confidence bound for mean net expectancy;
- profit factor >= 1.20;
- ambiguous outcomes <= 10% of all resolved samples.

REVIEW_READY never changes execution permissions. It only means there is enough
observed evidence to justify a separate code review of a possible live policy.
"""
from __future__ import annotations

import asyncio
import json
import math
import statistics
import time
from typing import Any

from bot import database as db

_TABLE_READY = False
_TABLE_LOCK = asyncio.Lock()
_MIN_RESOLVED = 40
_MIN_PROFIT_FACTOR = 1.20
_MAX_AMBIGUOUS_RATE = 0.10
_ONE_SIDED_Z95 = 1.645


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def compute_readiness(net_values, *, ambiguous: int = 0, resolved_total: int | None = None):
    """Return deterministic calibration metrics from resolved net outcomes."""
    vals = [_finite(v) for v in net_values if math.isfinite(_finite(v))]
    n = len(vals)
    total = int(resolved_total if resolved_total is not None else n + int(ambiguous))
    mean = statistics.fmean(vals) if vals else 0.0
    stdev = statistics.stdev(vals) if n >= 2 else 0.0
    se = stdev / math.sqrt(n) if n >= 2 else float("inf")
    lower95 = mean - _ONE_SIDED_Z95 * se if math.isfinite(se) else float("-inf")
    gains = sum(v for v in vals if v > 0)
    losses = abs(sum(v for v in vals if v < 0))
    profit_factor = gains / losses if losses > 0 else (float("inf") if gains > 0 else 0.0)
    wins = sum(1 for v in vals if v > 0)
    ambiguous_rate = (int(ambiguous) / total) if total > 0 else 0.0
    review_ready = (
        n >= _MIN_RESOLVED
        and mean > 0
        and lower95 > 0
        and profit_factor >= _MIN_PROFIT_FACTOR
        and ambiguous_rate <= _MAX_AMBIGUOUS_RATE
    )
    return {
        "resolved_non_ambiguous": n,
        "resolved_total": total,
        "ambiguous": int(ambiguous),
        "ambiguous_rate": round(ambiguous_rate, 5),
        "mean_net_pct": round(mean, 5),
        "lower95_net_pct": round(lower95, 5) if math.isfinite(lower95) else None,
        "profit_factor": round(profit_factor, 4) if math.isfinite(profit_factor) else None,
        "win_rate": round(wins / n, 5) if n else 0.0,
        "review_ready": bool(review_ready),
        "execution_effect": "NONE",
    }


async def _ensure_table(log) -> bool:
    global _TABLE_READY
    if _TABLE_READY:
        return True
    async with _TABLE_LOCK:
        if _TABLE_READY:
            return True
        ok = await db._exec("""CREATE TABLE IF NOT EXISTS htf_transition_audit (
            state_id TEXT PRIMARY KEY,
            created_epoch REAL,
            resolved_epoch REAL,
            symbol TEXT,
            direction TEXT,
            entry_type TEXT,
            score REAL,
            score_4h_forming REAL,
            score_1h REAL,
            score_15m REAL,
            volume_ratio_15m REAL,
            adx_15m REAL,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            outcome TEXT,
            net_pct REAL,
            mfe_pct REAL,
            mae_pct REAL,
            bars INTEGER,
            metadata TEXT
        )""")
        if ok:
            await db._exec(
                "CREATE INDEX IF NOT EXISTS idx_htf_transition_resolved "
                "ON htf_transition_audit(resolved_epoch)"
            )
            await db._exec(
                "CREATE INDEX IF NOT EXISTS idx_htf_transition_symbol "
                "ON htf_transition_audit(symbol)"
            )
            _TABLE_READY = True
            log.info(
                "[HTF_TRANSITION_CALIBRATION] persistent evidence table ready; "
                "execution_effect=NONE"
            )
        return bool(ok)


async def _record_open(payload: dict, log) -> None:
    if not await _ensure_table(log):
        return
    metadata = json.dumps({
        "closed_4h_state": payload.get("closed_4h_state"),
        "forming_4h_state": payload.get("forming_4h_state"),
        "confirmed_1h_state": payload.get("confirmed_1h_state"),
        "confirmed_15m_state": payload.get("confirmed_15m_state"),
        "policy": payload.get("policy"),
    }, separators=(",", ":"), sort_keys=True)
    await db._exec(
        """INSERT INTO htf_transition_audit (
        state_id,created_epoch,symbol,direction,entry_type,score,
        score_4h_forming,score_1h,score_15m,volume_ratio_15m,adx_15m,
        entry_price,stop_loss,take_profit,metadata
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(state_id) DO NOTHING""",
        (
            str(payload.get("state_id")), time.time(), str(payload.get("symbol")),
            str(payload.get("direction")), str(payload.get("entry_type")),
            _finite(payload.get("score")), _finite(payload.get("score_4h_forming")),
            _finite(payload.get("score_1h")), _finite(payload.get("score_15m")),
            _finite(payload.get("volume_ratio_15m")), _finite(payload.get("adx_15m")),
            _finite(payload.get("entry")), _finite(payload.get("sl")),
            _finite(payload.get("tp")), metadata,
        ),
    )


async def _calibration_report(log) -> dict:
    if not await _ensure_table(log):
        return compute_readiness([])
    rows = await db._fetchall(
        "SELECT outcome,net_pct FROM htf_transition_audit WHERE resolved_epoch IS NOT NULL"
    )
    nets = []
    ambiguous = 0
    for row in rows or []:
        outcome = str(row[0] or "")
        if outcome == "AMBIGUOUS_BOTH":
            ambiguous += 1
            continue
        if outcome in {"TP", "SL", "TIMEOUT_4H"}:
            nets.append(_finite(row[1]))
    metrics = compute_readiness(nets, ambiguous=ambiguous, resolved_total=len(rows or []))
    log.info(
        "[HTF_TRANSITION_CALIBRATION] samples=%d total=%d mean_net=%s "
        "lower95=%s pf=%s ambiguous_rate=%.3f review_ready=%s execution_effect=NONE",
        metrics["resolved_non_ambiguous"], metrics["resolved_total"],
        metrics["mean_net_pct"], metrics["lower95_net_pct"],
        metrics["profit_factor"], metrics["ambiguous_rate"],
        metrics["review_ready"],
    )
    return metrics


async def _record_outcome(payload: dict, log) -> None:
    if not await _ensure_table(log):
        return
    await db._exec(
        """UPDATE htf_transition_audit SET
        resolved_epoch=?,outcome=?,net_pct=?,mfe_pct=?,mae_pct=?,bars=?
        WHERE state_id=?""",
        (
            time.time(), str(payload.get("outcome")),
            _finite(payload.get("net_pct_after_cost_model")),
            _finite(payload.get("mfe_pct")), _finite(payload.get("mae_pct")),
            int(payload.get("bars") or 0), str(payload.get("state_id")),
        ),
    )
    await _calibration_report(log)


def _schedule(coro, log) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.debug(
            "[HTF_TRANSITION_CALIBRATION] no_running_loop persistence_skipped "
            "execution_effect=NONE"
        )
        return
    task = loop.create_task(coro)

    def _done(done):
        try:
            done.result()
        except Exception as exc:
            log.warning(
                "[HTF_TRANSITION_CALIBRATION] persistence_error=%s execution_effect=NONE",
                type(exc).__name__,
            )
    task.add_done_callback(_done)


def schedule_open(payload: dict, log) -> None:
    """Persist a shadow transition sample without blocking strategy execution."""
    _schedule(_record_open(dict(payload), log), log)


def schedule_outcome(payload: dict, log) -> None:
    """Persist a resolved outcome and recompute review readiness."""
    _schedule(_record_outcome(dict(payload), log), log)
