"""Passive post-trade forensic telemetry.

This module measures trade-path quality without changing signals, sizing,
thresholds, protection, exchange routing, or close decisions. It tracks MFE/MAE
on the canonical Position object and emits a structured report when a position
disappears from the exchange during normal reconciliation.
"""
from __future__ import annotations

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
        now = datetime.utcnow()
        return max(0.0, (now - opened_at).total_seconds() / 60.0)
    except Exception:
        return 0.0


def _roe_pct(pnl: float, entry: float, qty: float, leverage: float) -> float:
    notional = abs(_num(entry) * _num(qty))
    lev = max(1.0, _num(leverage, 1.0))
    margin = notional / lev
    return (_num(pnl) / margin * 100.0) if margin > 0 else 0.0


def install(TradingEngine, Position, cfg, fee_rate, log) -> None:
    """Install passive MFE/MAE tracking and closure reports."""
    if getattr(TradingEngine, "_post_trade_forensics_installed", False):
        return

    original_update = Position.update_pnl

    def _update_pnl_forensics(self, current_price):
        result = original_update(self, current_price)
        pnl = _num(getattr(self, "pnl", 0.0))
        price = _num(getattr(self, "current_price", current_price))
        if not hasattr(self, "_forensic_mfe_pnl"):
            self._forensic_mfe_pnl = pnl
            self._forensic_mae_pnl = pnl
            self._forensic_best_price = price
            self._forensic_worst_price = price
        else:
            if pnl > _num(self._forensic_mfe_pnl, pnl):
                self._forensic_mfe_pnl = pnl
                self._forensic_best_price = price
            if pnl < _num(self._forensic_mae_pnl, pnl):
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
                # External/read-only quarantine or non-trade state changes are not
                # reported as a completed BGX trade.
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
            be_price = _net_breakeven(entry, getattr(pos, "direction", ""), fee_rate)
            direction = str(getattr(pos, "direction", "")).upper()
            be_reached = (
                best_price >= be_price if direction == "LONG"
                else best_price <= be_price if direction == "SHORT"
                else False
            )
            sl = _num(getattr(pos, "sl", 0.0))
            tp = _num(getattr(pos, "tp", 0.0))
            tol = max(abs(entry) * 0.0005, 1e-12)
            if sl > 0 and abs(exit_price - sl) <= tol:
                inferred_exit = "SL_NEAR"
            elif tp > 0 and abs(exit_price - tp) <= tol:
                inferred_exit = "TP_NEAR"
            else:
                inferred_exit = "EXCHANGE_CLOSE_OTHER"

            nexus = (getattr(self, "_last_nexus", {}) or {}).get(sym, {})
            if not isinstance(nexus, dict):
                nexus = {}
            nexus_decision = str(nexus.get("decision", nexus.get("action", "UNKNOWN")))
            score = _num(getattr(pos, "score", 0.0))
            regime = str(getattr(pos, "regime", "UNKNOWN") or "UNKNOWN")
            entry_type = str(getattr(pos, "entry_type", "UNKNOWN") or "UNKNOWN")
            duration_min = _minutes(getattr(pos, "opened_at", None))

            capture = (pnl_net / mfe * 100.0) if mfe > 0 else 0.0
            log.warning(
                "[POST_TRADE_FORENSICS] symbol=%s side=%s entry=%.8f exit=%.8f qty=%.8f "
                "gross_pnl=%.6f net_pnl=%.6f fees=%.6f duration_min=%.1f "
                "mfe_pnl=%.6f mae_pnl=%.6f mfe_roe_pct=%.2f mae_roe_pct=%.2f "
                "best_price=%.8f worst_price=%.8f net_breakeven=%.8f breakeven_reached=%s "
                "profit_capture_pct=%.2f score=%.1f nexus=%s regime=%s entry_type=%s "
                "exit_inferred=%s sl=%.8f tp=%.8f slippage_bps=NA funding=NA "
                "decision_effect=NONE execution_effect=NONE",
                sym, direction, entry, exit_price, qty,
                pnl_gross, pnl_net, fees, duration_min,
                mfe, mae, _roe_pct(mfe, entry, qty, leverage), _roe_pct(mae, entry, qty, leverage),
                best_price, worst_price, be_price, str(bool(be_reached)).lower(),
                capture, score, nexus_decision, regime, entry_type,
                inferred_exit, sl, tp,
            )

        return result

    TradingEngine._sync_positions = _sync_positions_forensics
    TradingEngine._post_trade_forensics_installed = True
    log.info(
        "[POST_TRADE_FORENSICS] installed=true telemetry_only=true "
        "thresholds_unchanged=true leverage_unchanged=true execution_effect=NONE"
    )
