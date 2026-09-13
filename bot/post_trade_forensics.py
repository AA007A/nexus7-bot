"""Passive post-trade forensic telemetry.

Tracks MFE/MAE and preserves the decision lineage that existed at entry so
later exchange accounting can attribute a reconciled fill to the exact NEXUS,
regime and entry-type context. This module never changes trading decisions.
"""
from __future__ import annotations

import json
import math
from datetime import datetime


def _num(value, default=0.0):
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _net_breakeven(entry: float, direction: str, fee_rate: float) -> float:
    entry = _num(entry)
    fee_rate = max(0.0, _num(fee_rate))
    if entry <= 0 or fee_rate >= 1:
        return 0.0
    if str(direction).upper() == "LONG":
        return entry * (1.0 + fee_rate) / (1.0 - fee_rate)
    return entry * (1.0 - fee_rate) / (1.0 + fee_rate)


def _minutes(opened_at) -> float:
    if not isinstance(opened_at, datetime):
        return 0.0
    try:
        return max(0.0, (datetime.utcnow() - opened_at).total_seconds() / 60.0)
    except Exception:
        return 0.0


def _roe_pct(pnl: float, entry: float, qty: float, leverage: float) -> float:
    notional = abs(_num(entry) * _num(qty))
    lev = max(1.0, _num(leverage, 1.0))
    margin = notional / lev
    return (_num(pnl) / margin * 100.0) if margin > 0 else 0.0


def _lineage(sig, nexus):
    nexus = nexus if isinstance(nexus, dict) else {}
    return {
        "version": 1,
        "symbol": str(getattr(sig, "symbol", "")),
        "direction": str(getattr(sig, "direction", "")),
        "entry": _num(getattr(sig, "entry", 0.0)),
        "score": _num(getattr(sig, "score", 0.0)),
        "regime": str(getattr(sig, "regime", "UNKNOWN") or "UNKNOWN"),
        "entry_type": str(getattr(sig, "entry_type", "UNKNOWN") or "UNKNOWN"),
        "nexus": str(nexus.get("decision", nexus.get("action", "UNKNOWN"))),
        "captured_at": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
    }


async def _persist_lineage(symbol, payload, log):
    try:
        from bot import database as db
        from bot.durable_daily_stop import state_key
        key = state_key("trade_lineage") + ":" + str(symbol)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if await db.save_key_value(key, encoded, strict=True) is not True:
            raise db.PersistenceError("lineage persistence unconfirmed")
        log.info("[TRADE_LINEAGE] symbol=%s durable=true nexus=%s regime=%s entry_type=%s execution_effect=NONE",
                 symbol, payload["nexus"], payload["regime"], payload["entry_type"])
    except Exception as exc:
        log.warning("[TRADE_LINEAGE] symbol=%s durable=false error=%s execution_effect=NONE",
                    symbol, type(exc).__name__)


def install(TradingEngine, Position, cfg, fee_rate, log) -> None:
    """Install passive entry-lineage capture, MFE/MAE tracking and closure reports."""
    if getattr(TradingEngine, "_post_trade_forensics_installed", False):
        return

    original_open = TradingEngine._open

    async def _open_with_lineage(self, sig, *args, **kwargs):
        symbol = str(getattr(sig, "symbol", ""))
        nexus = (getattr(self, "_last_nexus", {}) or {}).get(symbol, {})
        payload = _lineage(sig, nexus)
        result = await original_open(self, sig, *args, **kwargs)
        pos = (getattr(self, "positions", {}) or {}).get(symbol)
        if pos is not None:
            pos._forensic_lineage = payload
            await _persist_lineage(symbol, payload, log)
        return result

    TradingEngine._open = _open_with_lineage

    original_update = Position.update_pnl

    def _update_pnl_forensics(self, current_price):
        result = original_update(self, current_price)
        pnl = _num(getattr(self, "pnl", 0.0))
        price = _num(getattr(self, "current_price", current_price))
        entry = _num(getattr(self, "entry", price), price)
        if not hasattr(self, "_forensic_mfe_pnl"):
            self._forensic_mfe_pnl = max(0.0, pnl)
            self._forensic_mae_pnl = min(0.0, pnl)
            self._forensic_best_price = price if pnl > 0 else entry
            self._forensic_worst_price = price if pnl < 0 else entry
        else:
            if pnl > _num(self._forensic_mfe_pnl, 0.0):
                self._forensic_mfe_pnl = pnl
                self._forensic_best_price = price
            if pnl < _num(self._forensic_mae_pnl, 0.0):
                self._forensic_mae_pnl = pnl
                self._forensic_worst_price = price
        return result

    Position.update_pnl = _update_pnl_forensics
    original_sync = TradingEngine._sync_positions

    async def _sync_positions_forensics(self, *args, **kwargs):
        before = dict(getattr(self, "positions", {}) or {})
        stats = getattr(self, "stats", None)
        trades_before = len(getattr(stats, "trades", []) or []) if stats is not None else 0
        result = await original_sync(self, *args, **kwargs)
        after = set((getattr(self, "positions", {}) or {}).keys())
        removed = [sym for sym in before if sym not in after]
        new_trades = list((getattr(stats, "trades", []) or [])[trades_before:]) if stats is not None else []

        for sym in removed:
            pos = before[sym]
            trade = next((t for t in reversed(new_trades) if getattr(t, "symbol", None) == sym), None)
            if trade is None:
                continue
            entry = _num(getattr(trade, "entry", getattr(pos, "entry", 0.0)))
            exit_price = _num(getattr(trade, "exit_price", getattr(pos, "current_price", 0.0)))
            qty = _num(getattr(trade, "qty", getattr(pos, "qty", 0.0)))
            pnl_gross = _num(getattr(trade, "pnl_gross", 0.0))
            pnl_net = _num(getattr(trade, "pnl", pnl_gross))
            fees = _num(getattr(trade, "total_fees", 0.0))
            mfe = _num(getattr(pos, "_forensic_mfe_pnl", getattr(pos, "peak_pnl", 0.0)))
            mae = _num(getattr(pos, "_forensic_mae_pnl", min(0.0, getattr(pos, "pnl", 0.0))))
            best_price = _num(getattr(pos, "_forensic_best_price", entry))
            worst_price = _num(getattr(pos, "_forensic_worst_price", entry))
            leverage = _num(getattr(cfg, "LEVERAGE", 1.0), 1.0)
            direction = str(getattr(pos, "direction", "")).upper()
            be_price = _net_breakeven(entry, direction, fee_rate)
            be_reached = best_price >= be_price if direction == "LONG" else best_price <= be_price if direction == "SHORT" else False
            sl, tp = _num(getattr(pos, "sl", 0.0)), _num(getattr(pos, "tp", 0.0))
            tol = max(abs(entry) * 0.0005, 1e-12)
            inferred_exit = "SL_NEAR" if sl > 0 and abs(exit_price - sl) <= tol else "TP_NEAR" if tp > 0 and abs(exit_price - tp) <= tol else "EXCHANGE_CLOSE_OTHER"
            lineage = getattr(pos, "_forensic_lineage", None)
            if not isinstance(lineage, dict):
                nexus = (getattr(self, "_last_nexus", {}) or {}).get(sym, {})
                lineage = _lineage(pos, nexus)
            capture = (pnl_net / mfe * 100.0) if mfe > 0 else 0.0
            log.warning(
                "[POST_TRADE_FORENSICS] symbol=%s side=%s entry=%.8f exit=%.8f qty=%.8f accounting_source=ESTIMATED_LOCAL_MARK_AND_FEE_RATE fills_confirmed=false gross_pnl=%.6f net_pnl=%.6f fees=%.6f duration_min=%.1f mfe_pnl=%.6f mae_pnl=%.6f mfe_roe_pct=%.2f mae_roe_pct=%.2f best_price=%.8f worst_price=%.8f net_breakeven=%.8f breakeven_reached=%s profit_capture_pct=%.2f score=%.1f nexus=%s regime=%s entry_type=%s exit_inferred=%s sl=%.8f tp=%.8f slippage_bps=NA funding=NA decision_effect=NONE execution_effect=NONE",
                sym, direction, entry, exit_price, qty, pnl_gross, pnl_net, fees, _minutes(getattr(pos, "opened_at", None)),
                mfe, mae, _roe_pct(mfe, entry, qty, leverage), _roe_pct(mae, entry, qty, leverage), best_price, worst_price,
                be_price, str(bool(be_reached)).lower(), capture, lineage["score"], lineage["nexus"], lineage["regime"], lineage["entry_type"], inferred_exit, sl, tp)
        return result

    TradingEngine._sync_positions = _sync_positions_forensics
    TradingEngine._post_trade_forensics_installed = True
    log.info("[POST_TRADE_FORENSICS] installed=true telemetry_only=true lineage_durable=true thresholds_unchanged=true leverage_unchanged=true execution_effect=NONE")
