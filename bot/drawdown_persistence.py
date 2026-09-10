"""Durable account-equity high-water mark for real-account drawdown.

The process-local RiskManager peak used to reset on every deploy/restart, which
made drawdown appear as 0% even when current account equity was below a prior
high. This module persists the peak in the existing database key_value table.

Safety invariants:
- real-account equity only; PAPER callers must not use this module;
- the durable peak never decreases;
- a missing key bootstraps from freshly verified account equity;
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


def _apply_peak(risk, peak: float, current_equity: float) -> None:
    """Apply one equity peak to both legacy and V3 risk state, without I/O."""
    # ProfessionalRiskAdapter exposes its V3 authority as _v3. Use the V3
    # public restoration method when present; legacy state remains the gate
    # called by the canonical engine before AI validation.
    v3 = getattr(risk, "_v3", None)
    if v3 is not None and hasattr(v3, "restore_peak_equity"):
        v3.restore_peak_equity(peak)

    legacy = getattr(risk, "_legacy", risk)
    existing = float(getattr(legacy, "peak_balance", 0.0) or 0.0)
    peak = max(existing, peak)
    legacy.peak_balance = peak
    legacy.drawdown = max(0.0, (peak - current_equity) / peak)


async def restore_update_real_account_peak(risk, equity: float, *, strict: bool = True) -> float:
    """Restore/update the durable high-water mark and recompute drawdown.

    The database is read on the first call per RiskManager instance. Later
    calls use the validated in-memory durable peak and only write when a new
    account-equity high is observed. A persistence failure while establishing
    or raising the peak propagates in strict mode so new entries fail closed.
    """
    equity = _positive_finite(equity, "account equity")

    cached = getattr(risk, _CACHE_ATTR, None)
    if cached is None:
        raw = await db.load_key_value(DURABLE_EQUITY_PEAK_KEY, strict=strict)
        if raw is None:
            persisted = None
        else:
            try:
                persisted = _positive_finite(raw, "persisted equity peak")
            except (TypeError, ValueError) as exc:
                raise db.PersistenceError("durable equity peak is malformed") from exc
    else:
        try:
            persisted = _positive_finite(cached, "cached equity peak")
        except (TypeError, ValueError) as exc:
            raise db.PersistenceError("cached durable equity peak is malformed") from exc

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
