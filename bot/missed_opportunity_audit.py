"""Persistent counterfactual audit for approved and rejected NEXUS candidates.

Observability only: this module never authorizes execution or changes positions,
orders, risk limits, thresholds, leverage or exchange settings.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from typing import Any

from bot import database as db
from bot.kucoin_execution_model import estimated_round_trip_cost_pct

_TABLE_READY = False
_TABLE_LOCK = asyncio.Lock()
_LAST_EVAL_MONO = 0.0
_EVAL_INTERVAL_S = 30.0
_HORIZONS = ((900, "p15_net_pct"), (1800, "p30_net_pct"),
             (3600, "p60_net_pct"), (7200, "p120_net_pct"),
             (14400, "p240_net_pct"))
_RECORDED_KEYS: set[str] = set()
_NEAR_MISS_LOGGED: set[str] = set()


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
    bucket = int(float(epoch) // 900)
    return f"{symbol}:{str(direction).upper()}:{entry_type or 'UNKNOWN'}:{bucket}"


def _blocker_class(reason: str) -> str:
    """Normalize NEXUS WAIT reasons for false-negative calibration."""
    text = str(reason or "").lower()
    if any(x in text for x in ("r:r", "rr ", "ev ", "expected value", "líquido", "liquido")):
        return "EV_RR"
    if any(x in text for x in ("regime", "compat=", "incompatível", "incompativel")):
        return "REGIME"
    if any(x in text for x in ("score", "threshold", "qualidade")):
        return "SCORE"
    if any(x in text for x in ("timeframe", "mtf", "diverge", "conflito")):
        return "MTF"
    if any(x in text for x in ("data", "dados", "stale", "antigos", "candles insuficientes")):
        return "DATA"
    if any(x in text for x in ("news", "notícia", "noticia", "macro")):
        return "NEWS_RISK"
    if "timeout" in text:
        return "TIMEOUT"
    return "OTHER"


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
    key = _signal_key(sig.symbol, sig.direction, getattr(sig, "entry_type", "UNKNOWN"), now)
    if key in _RECORDED_KEYS:
        return

    # Avoid repeated logs/DB writes when a deployment restarts inside the same
    # 15m bucket. The DB remains the durable dedupe authority.
    existing = await db._fetchall(
        "SELECT signal_key FROM opportunity_audit WHERE signal_key=? LIMIT 1", (key,)
    )
    if existing:
        _RECORDED_KEYS.add(key)
        return

    approved = 1 if getattr(decision, "execution_allowed", None) is True else 0
    reason = _decision_reason(decision)
    blocker = "APPROVED" if approved else _blocker_class(reason)
    estimated_cost = estimated_round_trip_cost_pct(sig.symbol)
    metadata = json.dumps({
        "rr": _finite(getattr(sig, "rr", 0.0)),
        "expected_pnl": _finite(getattr(sig, "expected_pnl", 0.0)),
        "signal_regime": str(getattr(sig, "regime", "UNKNOWN")),
        "tf_4h": str(getattr(sig, "tf_4h", "")),
        "tf_1h": str(getattr(sig, "tf_1h", "")),
        "tf_15m": str(getattr(sig, "tf_15m", "")),
        "blocker_class": blocker,
        "estimated_round_trip_cost_pct": round(estimated_cost, 5),
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
        reason,
        metadata,
    )
    inserted = await db._exec(sql, params)
    _RECORDED_KEYS.add(key)
    if inserted:
        log.info(
            "[OPPORTUNITY_AUDIT] candidate=%s symbol=%s side=%s approved=%s "
            "blocker=%s strategy_score=%.1f nexus_score=%.1f cost=%.3f%% "
            "entry=%.8f execution_effect=NONE",
            key, sig.symbol, sig.direction, bool(approved), blocker,
            _finite(getattr(sig, "score", 0.0)),
            _finite(getattr(decision, "setup_quality", 0.0)),
            estimated_cost, _finite(sig.entry),
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
                  hypothetical_status,approved,decision_reason,metadata
           FROM opportunity_audit
           WHERE created_epoch>=? AND (p240_net_pct IS NULL OR last_eval_epoch IS NULL)
           ORDER BY created_epoch ASC LIMIT 250""",
        (now - 18000.0,),
    )
    if not rows:
        return

    updated = 0
    near_miss_threshold = max(0.0, _finite(os.environ.get("OPPORTUNITY_NEAR_MISS_PCT", "0.50"), 0.50))
    for row in rows:
        try:
            (key, created_epoch, symbol, direction, entry, sl, tp,
             old_mfe, old_mae, p15, p30, p60, p120, p240, status,
             approved, decision_reason, metadata_raw) = row
            ticker = engine.client.get_cached_ticker(symbol) or {}
            current = _finite(ticker.get("lastPrice"))
            if current <= 0:
                continue
            cost_pct = estimated_round_trip_cost_pct(symbol)
            raw_pct = _directional_return_pct(direction, entry, current)
            net_pct = raw_pct - cost_pct
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

            net_mfe = mfe - cost_pct
            if not int(approved or 0) and net_mfe >= near_miss_threshold and key not in _NEAR_MISS_LOGGED:
                blocker = _blocker_class(decision_reason)
                try:
                    meta = json.loads(metadata_raw or "{}")
                    blocker = str(meta.get("blocker_class") or blocker)
                except Exception:
                    pass
                _NEAR_MISS_LOGGED.add(key)
                log.warning(
                    "[OPPORTUNITY_NEAR_MISS] symbol=%s side=%s blocker=%s "
                    "net_mfe=%.3f%% raw_mfe=%.3f%% est_cost=%.3f%% age_s=%.0f "
                    "execution_effect=NONE",
                    symbol, direction, blocker, net_mfe, mfe, cost_pct, age,
                )
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
