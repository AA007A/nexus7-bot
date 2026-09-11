"""Persistent counterfactual audit for approved and rejected NEXUS candidates.

This module is observability only. It never authorizes execution and never
changes a signal, position, order, risk limit, or exchange setting.

For each strategy candidate that reaches the NEXUS gate, one row per 15-minute
signal bucket is persisted. While the bot keeps scanning, cached ticker prices
are sampled to estimate the counterfactual path after the decision:

- directional MFE / MAE;
- directional net return after estimated round-trip trading costs;
- snapshots near +15m, +30m, +1h, +2h and +4h;
- whether the proposed SL or TP was observed crossing during sampling.

The purpose is to measure false negatives instead of lowering gates based on
hindsight or intuition. Sampling uses the existing in-process ticker cache and
therefore adds no exchange REST load. Results are diagnostic, not a backtest and
not proof that a rejected trade would have filled at the quoted price.
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
_LAST_EVAL_MONO = 0.0
_EVAL_INTERVAL_S = 30.0
_HORIZONS = ((900, "p15_net_pct"), (1800, "p30_net_pct"),
             (3600, "p60_net_pct"), (7200, "p120_net_pct"),
             (14400, "p240_net_pct"))
# KuCoin taker both sides (0.12%) + the NEXUS default slippage both sides (0.10%).
_ESTIMATED_ROUND_TRIP_COST_PCT = 0.22


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _directional_return_pct(direction: str, entry: float, current: float) -> float:
    entry = _finite(entry)
    current = _finite(current)
    if entry <= 0 or current <= 0:
        return 0.0
    raw = (current - entry) / entry * 100.0
    return raw if str(direction).upper() == "LONG" else -raw


def _signal_key(symbol: str, direction: str, entry_type: str, epoch: float) -> str:
    # The strategy uses confirmed 15m candles, so repeated scans within the same
    # candle represent the same opportunity rather than independent samples.
    bucket = int(float(epoch) // 900)
    return f"{symbol}:{str(direction).upper()}:{entry_type or 'UNKNOWN'}:{bucket}"


async def _ensure_table(log) -> bool:
    global _TABLE_READY
    if _TABLE_READY:
        return True
    async with _TABLE_LOCK:
        if _TABLE_READY:
            return True
        sql = """CREATE TABLE IF NOT EXISTS opportunity_audit (
            signal_key TEXT PRIMARY KEY,
            created_at TEXT,
            created_epoch REAL,
            symbol TEXT,
            direction TEXT,
            entry_type TEXT,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            strategy_score REAL,
            nexus_score REAL,
            nexus_confidence REAL,
            nexus_regime TEXT,
            approved INTEGER,
            decision_reason TEXT,
            mfe_pct REAL DEFAULT 0,
            mae_pct REAL DEFAULT 0,
            last_net_pct REAL DEFAULT 0,
            p15_net_pct REAL,
            p30_net_pct REAL,
            p60_net_pct REAL,
            p120_net_pct REAL,
            p240_net_pct REAL,
            hypothetical_status TEXT DEFAULT 'OPEN',
            hit_epoch REAL,
            last_eval_epoch REAL,
            metadata TEXT
        )"""
        ok = await db._exec(sql)
        if ok:
            await db._exec(
                "CREATE INDEX IF NOT EXISTS idx_opportunity_audit_created "
                "ON opportunity_audit(created_epoch)"
            )
            await db._exec(
                "CREATE INDEX IF NOT EXISTS idx_opportunity_audit_symbol "
                "ON opportunity_audit(symbol)"
            )
            _TABLE_READY = True
            log.info(
                "[OPPORTUNITY_AUDIT] persistent counterfactual table ready; "
                "execution_effect=NONE"
            )
        return bool(ok)


def _decision_reason(decision) -> str:
    reasoning = getattr(decision, "reasoning", None) or []
    if reasoning:
        return str(reasoning[-1])[:1000]
    warnings = getattr(decision, "warnings", None) or []
    if warnings:
        return str(warnings[-1])[:1000]
    return str(getattr(decision, "decision", "UNKNOWN"))[:1000]


async def _record(engine, sig, decision, log) -> None:
    if not await _ensure_table(log):
        return
    now = time.time()
    key = _signal_key(
        sig.symbol,
        sig.direction,
        getattr(sig, "entry_type", "UNKNOWN"),
        now,
    )
    approved = 1 if getattr(decision, "execution_allowed", None) is True else 0
    metadata = json.dumps({
        "rr": _finite(getattr(sig, "rr", 0.0)),
        "expected_pnl": _finite(getattr(sig, "expected_pnl", 0.0)),
        "signal_regime": str(getattr(sig, "regime", "UNKNOWN")),
        "tf_4h": str(getattr(sig, "tf_4h", "")),
        "tf_1h": str(getattr(sig, "tf_1h", "")),
        "tf_15m": str(getattr(sig, "tf_15m", "")),
    }, separators=(",", ":"), sort_keys=True)
    sql = """INSERT INTO opportunity_audit (
        signal_key,created_at,created_epoch,symbol,direction,entry_type,
        entry_price,stop_loss,take_profit,strategy_score,nexus_score,
        nexus_confidence,nexus_regime,approved,decision_reason,metadata
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(signal_key) DO NOTHING"""
    params = (
        key,
        __import__("datetime").datetime.fromtimestamp(
            now, tz=__import__("datetime").timezone.utc
        ).isoformat(),
        now,
        sig.symbol,
        sig.direction,
        getattr(sig, "entry_type", "UNKNOWN"),
        _finite(sig.entry),
        _finite(sig.sl),
        _finite(sig.tp),
        _finite(getattr(sig, "score", 0.0)),
        _finite(getattr(decision, "setup_quality", 0.0)),
        _finite(getattr(decision, "confidence", 0.0)),
        str(getattr(decision, "market_regime", "UNKNOWN")),
        approved,
        _decision_reason(decision),
        metadata,
    )
    inserted = await db._exec(sql, params)
    if inserted:
        log.info(
            "[OPPORTUNITY_AUDIT] candidate=%s symbol=%s side=%s approved=%s "
            "strategy_score=%.1f nexus_score=%.1f entry=%.8f execution_effect=NONE",
            key, sig.symbol, sig.direction, bool(approved),
            _finite(getattr(sig, "score", 0.0)),
            _finite(getattr(decision, "setup_quality", 0.0)),
            _finite(sig.entry),
        )


async def _evaluate_pending(engine, log) -> None:
    global _LAST_EVAL_MONO
    now_mono = time.monotonic()
    if now_mono - _LAST_EVAL_MONO < _EVAL_INTERVAL_S:
        return
    _LAST_EVAL_MONO = now_mono
    if not await _ensure_table(log):
        return

    now = time.time()
    rows = await db._fetchall(
        """SELECT signal_key,created_epoch,symbol,direction,entry_price,
                  stop_loss,take_profit,mfe_pct,mae_pct,
                  p15_net_pct,p30_net_pct,p60_net_pct,p120_net_pct,p240_net_pct,
                  hypothetical_status
           FROM opportunity_audit
           WHERE created_epoch>=? AND (p240_net_pct IS NULL OR last_eval_epoch IS NULL)
           ORDER BY created_epoch ASC LIMIT 250""",
        (now - 18000.0,),
    )
    if not rows:
        return

    updated = 0
    for row in rows:
        try:
            (key, created_epoch, symbol, direction, entry, sl, tp,
             old_mfe, old_mae, p15, p30, p60, p120, p240, status) = row
            ticker = engine.client.get_cached_ticker(symbol) or {}
            current = _finite(ticker.get("lastPrice"))
            if current <= 0:
                continue
            raw_pct = _directional_return_pct(direction, entry, current)
            net_pct = raw_pct - _ESTIMATED_ROUND_TRIP_COST_PCT
            mfe = max(_finite(old_mfe), raw_pct)
            mae = min(_finite(old_mae), raw_pct)
            age = max(0.0, now - _finite(created_epoch))
            horizon_values = [p15, p30, p60, p120, p240]
            for idx, (seconds, _) in enumerate(_HORIZONS):
                if age >= seconds and horizon_values[idx] is None:
                    horizon_values[idx] = round(net_pct, 5)

            new_status = status or "OPEN"
            hit_epoch = None
            if new_status == "OPEN":
                is_long = str(direction).upper() == "LONG"
                tp_hit = (is_long and current >= _finite(tp)) or (
                    not is_long and current <= _finite(tp)
                )
                sl_hit = (is_long and current <= _finite(sl)) or (
                    not is_long and current >= _finite(sl)
                )
                # If an instantaneous sample somehow satisfies both, do not
                # guess event ordering; mark ambiguous instead of inventing PnL.
                if tp_hit and sl_hit:
                    new_status = "AMBIGUOUS"
                    hit_epoch = now
                elif tp_hit:
                    new_status = "TP_OBSERVED"
                    hit_epoch = now
                elif sl_hit:
                    new_status = "SL_OBSERVED"
                    hit_epoch = now

            await db._exec(
                """UPDATE opportunity_audit SET
                   mfe_pct=?,mae_pct=?,last_net_pct=?,p15_net_pct=?,
                   p30_net_pct=?,p60_net_pct=?,p120_net_pct=?,p240_net_pct=?,
                   hypothetical_status=?,hit_epoch=COALESCE(hit_epoch,?),last_eval_epoch=?
                   WHERE signal_key=?""",
                (round(mfe, 5), round(mae, 5), round(net_pct, 5),
                 horizon_values[0], horizon_values[1], horizon_values[2],
                 horizon_values[3], horizon_values[4], new_status, hit_epoch,
                 now, key),
            )
            updated += 1
        except Exception as exc:
            log.debug(
                "[OPPORTUNITY_AUDIT] evaluation skipped key=%s error=%s",
                row[0] if row else "unknown", type(exc).__name__,
            )
    if updated:
        log.info(
            "[OPPORTUNITY_AUDIT] evaluated=%d pending counterfactual paths; "
            "sample_source=cached_ticker execution_effect=NONE",
            updated,
        )


async def observe(engine, sig, decision, log) -> None:
    """Best-effort telemetry; any failure is isolated from trading decisions."""
    try:
        await _evaluate_pending(engine, log)
        await _record(engine, sig, decision, log)
    except Exception as exc:
        log.warning(
            "[OPPORTUNITY_AUDIT] telemetry_error=%s execution_effect=NONE",
            type(exc).__name__,
        )
