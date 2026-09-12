"""Live NEXUS execution-cost calibration for BGX.

This hardening changes only the cost inputs used by NEXUS expected-value math.
It does NOT change score thresholds, R:R thresholds, leverage, risk sizing,
position limits, order routing, or any execution permission.

When live market/account cost data are unavailable, behavior falls back exactly
to the legacy NEXUS assumptions: 6 bps taker fee and 5 bps one-way slippage.
"""
from __future__ import annotations

import contextvars
import functools
import math
import time
from dataclasses import dataclass
from typing import Any

from bot.kucoin_execution_model import (
    DEFAULT_SLIPPAGE,
    DEFAULT_TAKER_FEE,
    fetch_actual_taker_fee,
)


@dataclass(frozen=True)
class NexusCostContext:
    symbol: str
    taker_fee: float
    slippage: float
    fee_source: str
    slippage_source: str
    spread_bps: float | None = None


_COST_CONTEXT: contextvars.ContextVar[NexusCostContext | None] = contextvars.ContextVar(
    "bgx_nexus_cost_context", default=None
)
_FEE_CACHE: dict[str, tuple[float, str, float]] = {}
_FEE_CACHE_TTL_S = 3600.0


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _is_major(symbol: str) -> bool:
    sym = str(symbol or "").upper()
    return any(base in sym for base in ("BTC", "ETH", "SOL"))


def slippage_from_ticker(ticker: dict | None, symbol: str) -> tuple[float, str, float | None]:
    """Estimate one-way market slippage from current bid/ask, conservatively."""
    fallback = DEFAULT_SLIPPAGE
    if not isinstance(ticker, dict):
        return fallback, "legacy_fallback", None

    bid = _finite(ticker.get("bid"))
    ask = _finite(ticker.get("ask"))
    if bid <= 0 or ask <= 0 or ask < bid:
        return fallback, "legacy_fallback", None

    mid = (bid + ask) / 2.0
    if mid <= 0:
        return fallback, "legacy_fallback", None

    full_spread = (ask - bid) / mid
    spread_bps = full_spread * 10_000.0
    impact_floor = 0.00010 if _is_major(symbol) else 0.00020
    estimate = (full_spread / 2.0) + impact_floor
    estimate = min(max(estimate, impact_floor), 0.01)
    return estimate, "ticker_half_spread_plus_impact", spread_bps


async def _cached_taker_fee(client, symbol: str) -> tuple[float, str]:
    now = time.monotonic()
    cached = _FEE_CACHE.get(symbol)
    if cached and cached[2] > now:
        return cached[0], cached[1]

    try:
        fee, source = await fetch_actual_taker_fee(client, symbol)
    except Exception:
        fee, source = DEFAULT_TAKER_FEE, "legacy_fallback"

    fee = _finite(fee, DEFAULT_TAKER_FEE)
    if fee < 0 or fee >= 0.02:
        fee, source = DEFAULT_TAKER_FEE, "legacy_fallback"
    _FEE_CACHE[symbol] = (fee, source, now + _FEE_CACHE_TTL_S)
    return fee, source


async def build_cost_context(engine, sig) -> NexusCostContext:
    """Build read-only cost inputs for one NEXUS candidate."""
    symbol = str(sig.symbol)
    fee, fee_source = await _cached_taker_fee(engine.client, symbol)
    ticker = engine.client.get_cached_ticker(symbol) or {}
    slippage, slip_source, spread_bps = slippage_from_ticker(ticker, symbol)
    return NexusCostContext(
        symbol=symbol,
        taker_fee=fee,
        slippage=slippage,
        fee_source=fee_source,
        slippage_source=slip_source,
        spread_bps=spread_bps,
    )


def _attach_cost_context(decision, ctx: NexusCostContext, log) -> bool:
    """Attach private telemetry when the returned decision supports attributes.

    Production NexusDecision objects support dynamic private attributes. Some
    compatibility/unit-test validators return plain dicts; those must keep their
    exact return value and must never fail because observability cannot attach.
    """
    try:
        setattr(decision, "_bgx_nexus_cost_context", ctx)
        return True
    except (AttributeError, TypeError):
        debug = getattr(log, "debug", None)
        if callable(debug):
            debug(
                "[NEXUS_COST] context_handoff_unsupported symbol=%s decision_type=%s "
                "decision_effect=NONE execution_effect=NONE",
                ctx.symbol,
                type(decision).__name__,
            )
        return False


def install(TradingEngine, nexus_ai, log) -> None:
    """Install fail-safe context propagation around the existing NEXUS gate."""
    if getattr(TradingEngine, "_bgx_nexus_cost_calibration_installed", False):
        return

    original_validate = TradingEngine._nexus_validate
    original_expected_value = nexus_ai.expected_value

    @functools.wraps(original_expected_value)
    def calibrated_expected_value(
        win_prob: float,
        entry: float,
        sl: float,
        tp: float,
        taker_fee: float = DEFAULT_TAKER_FEE,
        slippage: float = DEFAULT_SLIPPAGE,
    ) -> dict:
        ctx = _COST_CONTEXT.get()
        if ctx is None:
            return original_expected_value(
                win_prob, entry, sl, tp, taker_fee=taker_fee, slippage=slippage
            )

        result = original_expected_value(
            win_prob,
            entry,
            sl,
            tp,
            taker_fee=ctx.taker_fee,
            slippage=ctx.slippage,
        )
        if isinstance(result, dict):
            result = dict(result)
            result["taker_fee"] = round(ctx.taker_fee, 8)
            result["slippage"] = round(ctx.slippage, 8)
            result["cost_source"] = f"{ctx.fee_source}+{ctx.slippage_source}"
            result["spread_bps"] = None if ctx.spread_bps is None else round(ctx.spread_bps, 4)
        return result

    @functools.wraps(original_validate)
    async def calibrated_validate(self, sig):
        try:
            ctx = await build_cost_context(self, sig)
        except Exception as exc:
            ctx = NexusCostContext(
                symbol=str(getattr(sig, "symbol", "UNKNOWN")),
                taker_fee=DEFAULT_TAKER_FEE,
                slippage=DEFAULT_SLIPPAGE,
                fee_source="legacy_fallback",
                slippage_source="legacy_fallback",
                spread_bps=None,
            )
            log.warning(
                "[NEXUS_COST] symbol=%s calibration_error=%s fallback=true "
                "thresholds_unchanged=true",
                ctx.symbol,
                type(exc).__name__,
            )

        token = _COST_CONTEXT.set(ctx)
        try:
            log.info(
                "[NEXUS_COST] symbol=%s taker_bps=%.3f slippage_bps=%.3f "
                "spread_bps=%s fee_source=%s slippage_source=%s "
                "thresholds_unchanged=true leverage_unchanged=true",
                ctx.symbol,
                ctx.taker_fee * 10_000.0,
                ctx.slippage * 10_000.0,
                "NA" if ctx.spread_bps is None else f"{ctx.spread_bps:.3f}",
                ctx.fee_source,
                ctx.slippage_source,
            )
            decision = await original_validate(self, sig)

            # Runtime post-decision observability executes after this wrapper
            # returns and after the ContextVar is reset. Production decisions
            # therefore carry the exact frozen context as private telemetry.
            # NexusDecision.to_dict()/asdict does not serialize dynamic attrs.
            _attach_cost_context(decision, ctx, log)
            return decision
        finally:
            _COST_CONTEXT.reset(token)

    nexus_ai.expected_value = calibrated_expected_value
    TradingEngine._nexus_validate = calibrated_validate
    TradingEngine._bgx_nexus_cost_calibration_installed = True

    log.warning(
        "[NEXUS_COST_CALIBRATION] installed actual_taker_fee=true "
        "ticker_spread_slippage=true fee_cache_ttl_s=3600 "
        "fallback_taker_bps=6 fallback_slippage_bps=5 "
        "rr_threshold_unchanged=true score_threshold_unchanged=true "
        "leverage_unchanged=true"
    )