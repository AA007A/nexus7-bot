"""Durable, cash-flow-aware account-equity high-water mark for LIVE drawdown.

The process-local RiskManager peak used to reset on every deploy/restart, which
made drawdown appear as 0% even when current account equity was below a prior
high. This module persists the high-water mark in the existing database
key_value table.

Safety invariants:
- real-account equity only; PAPER callers must not use this module;
- ordinary market/trading updates never lower the durable peak;
- only an explicitly verified external capital flow may rebase the peak lower;
- external-flow rebasing is multiplicative so the pre-flow drawdown percentage
  is preserved across deposits/withdrawals/transfers;
- malformed/unavailable durable state fails closed when strict=True;
- no exchange mutation or execution authorization exists here.
"""
from __future__ import annotations

import math

from bot import database as db
from bot.logger import log

DURABLE_EQUITY_PEAK_KEY = "risk:account_equity_peak:v1"
_CACHE_ATTR = "_durable_account_equity_peak"


def _positive_finite(value, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} boolean")
    out = float(value)
    if not math.isfinite(out) or out <= 0:
        raise ValueError(f"{label} must be positive and finite")
    return out


def _apply_peak(risk, peak: float, current_equity: float, *, allow_lower: bool = False) -> None:
    """Apply one equity peak to both legacy and V3 risk state, without I/O."""
    peak = _positive_finite(peak, "equity peak")
    current_equity = _positive_finite(current_equity, "account equity")

    v3 = getattr(risk, "_v3", None)
    if v3 is not None:
        if allow_lower and hasattr(v3, "rebase_peak_equity"):
            v3.rebase_peak_equity(peak)
        elif hasattr(v3, "restore_peak_equity"):
            # restore_peak_equity intentionally never lowers an existing HWM.
            # For cash-flow rebases on adapters whose V3 does not yet expose a
            # lower-capital rebase hook, update its private peak only after the
            # flow has been explicitly verified by the caller.
            if allow_lower and hasattr(v3, "_peak_equity"):
                v3._peak_equity = peak
            else:
                v3.restore_peak_equity(peak)

    legacy = getattr(risk, "_legacy", risk)
    if allow_lower:
        legacy.peak_balance = peak
    else:
        existing = float(getattr(legacy, "peak_balance", 0.0) or 0.0)
        legacy.peak_balance = max(existing, peak)
        peak = legacy.peak_balance
    legacy.drawdown = max(0.0, (peak - current_equity) / peak)


async def _load_peak(risk, *, strict: bool) -> tuple[float | None, str]:
    cached = getattr(risk, _CACHE_ATTR, None)
    if cached is not None:
        try:
            return _positive_finite(cached, "cached equity peak"), "cache"
        except (TypeError, ValueError) as exc:
            raise db.PersistenceError("cached durable equity peak is malformed") from exc

    raw = await db.load_key_value(DURABLE_EQUITY_PEAK_KEY, strict=strict)
    if raw is None:
        return None, "database"
    try:
        return _positive_finite(raw, "persisted equity peak"), "database"
    except (TypeError, ValueError) as exc:
        raise db.PersistenceError("durable equity peak is malformed") from exc


async def restore_update_real_account_peak(risk, equity: float, *, strict: bool = True) -> float:
    """Restore/update the durable high-water mark and recompute drawdown."""
    equity = _positive_finite(equity, "account equity")
    persisted, _ = await _load_peak(risk, strict=strict)

    peak = max(equity, persisted or equity)
    needs_write = persisted is None or peak > persisted
    if needs_write:
        ok = await db.save_key_value(
            DURABLE_EQUITY_PEAK_KEY,
            format(peak, ".17g"),
            strict=strict,
        )
        if strict and not ok:
            raise db.PersistenceError("durable equity peak write not confirmed")

    setattr(risk, _CACHE_ATTR, peak)
    _apply_peak(risk, peak, equity)

    log.info(
        "[DURABLE_DRAWDOWN] equity=%.4f peak_equity=%.4f drawdown=%.2f%% "
        "source=%s persistence=%s execution_effect=NONE",
        equity,
        peak,
        max(0.0, (peak - equity) / peak) * 100.0,
        "bootstrap" if persisted is None else "restored",
        "updated" if needs_write else "unchanged",
    )
    return peak


async def rebase_real_account_peak_for_external_flow(
    risk,
    current_equity: float,
    *,
    pre_flow_equity: float,
    post_flow_equity: float,
    flow_type: str,
    flow_amount: float,
    flow_offset: str,
    strict: bool = True,
) -> float:
    """Rebase the durable HWM after a verified external capital flow.

    The ratio post_flow_equity/pre_flow_equity is applied to the existing HWM.
    This preserves the drawdown percentage that existed immediately before the
    cash flow. A withdrawal therefore cannot masquerade as a trading loss, and
    a deposit cannot erase prior drawdown.

    This is the *only* path allowed to lower the durable peak and must only be
    called after the exchange ledger proves a completed TransferIn/TransferOut.
    """
    current_equity = _positive_finite(current_equity, "account equity")
    pre_flow_equity = _positive_finite(pre_flow_equity, "pre-flow equity")
    post_flow_equity = _positive_finite(post_flow_equity, "post-flow equity")
    flow_amount = _positive_finite(flow_amount, "external flow amount")

    persisted, _ = await _load_peak(risk, strict=strict)
    if persisted is None:
        raise db.PersistenceError("cannot rebase missing durable equity peak")

    ratio = post_flow_equity / pre_flow_equity
    if not math.isfinite(ratio) or ratio <= 0:
        raise ValueError("external flow ratio must be positive and finite")

    rebased_peak = max(current_equity, persisted * ratio)
    ok = await db.save_key_value(
        DURABLE_EQUITY_PEAK_KEY,
        format(rebased_peak, ".17g"),
        strict=strict,
    )
    if strict and not ok:
        raise db.PersistenceError("cash-flow-adjusted peak write not confirmed")

    setattr(risk, _CACHE_ATTR, rebased_peak)
    _apply_peak(risk, rebased_peak, current_equity, allow_lower=True)

    log.warning(
        "[CAPITAL_FLOW_REBASE] type=%s amount=%.4f offset=%s pre_equity=%.4f "
        "post_equity=%.4f old_peak=%.4f new_peak=%.4f drawdown=%.2f%% "
        "execution_effect=NONE",
        flow_type,
        flow_amount,
        flow_offset,
        pre_flow_equity,
        post_flow_equity,
        persisted,
        rebased_peak,
        max(0.0, (rebased_peak - current_equity) / rebased_peak) * 100.0,
    )
    return rebased_peak
