"""Remove contradictory soft-score authority after an exact NEXUS approval.

The legacy ``score.calculate`` system predates the NEXUS decision engine. In the
controlled LIVE pilot the core engine already requires an exact, fail-closed
``NexusDecision.execution_allowed is True`` for the same symbol, side and trade
levels before it reaches the legacy score. RiskManagerV3, PilotGuard, market
risk, drawdown, ownership, durable execution and the final spread/depth/drift
guard remain independent downstream authorities.

This module preserves the legacy score and all of its telemetry, but makes its
boolean ``aprovado`` field advisory only when the current LIVE opening task can
prove that the exact same trade has already received a valid NEXUS approval.
Any missing, stale, mismatched or negative NEXUS record leaves the legacy gate
unchanged. PAPER and non-pilot paths are unchanged.
"""
from __future__ import annotations

import contextvars
import math
from typing import Any


_ENGINE = contextvars.ContextVar("nexus_legacy_pretrade_engine", default=None)
_SIGNAL = contextvars.ContextVar("nexus_legacy_pretrade_signal", default=None)


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _exact_nexus_approval(engine, sig, symbol: str, direction: str) -> dict | None:
    """Return the stored NEXUS decision only for the exact authorized trade."""
    decisions = getattr(engine, "_last_nexus", None)
    if not isinstance(decisions, dict):
        return None
    decision = decisions.get(symbol)
    if not isinstance(decision, dict):
        return None

    # Exact bool True is the global NEXUS authorization invariant.
    if decision.get("execution_allowed") is not True:
        return None
    if decision.get("symbol") != symbol:
        return None
    if str(decision.get("decision", "")).upper() != str(direction).upper():
        return None

    sig_entry = _finite(getattr(sig, "entry", None))
    sig_sl = _finite(getattr(sig, "sl", None))
    sig_tp = _finite(getattr(sig, "tp", None))
    dec_entry = _finite(decision.get("entry"))
    dec_sl = _finite(decision.get("stop_loss"))
    dec_tp = _finite(decision.get("take_profit"))
    if None in (sig_entry, sig_sl, sig_tp, dec_entry, dec_sl, dec_tp):
        return None

    # Core decision_validation_error already enforces equality before the record
    # is stored. Re-check here so a stale previous decision cannot grant authority.
    if (dec_entry, dec_sl, dec_tp) != (sig_entry, sig_sl, sig_tp):
        return None

    score = _finite(decision.get("setup_quality"))
    if score is None:
        return None
    return decision


def install(TradingEngine, scoring, log) -> None:
    if getattr(TradingEngine, "_legacy_pretrade_advisory_installed", False):
        return

    original_open = TradingEngine._open
    original_calculate = scoring.calculate

    async def _open_with_context(self, sig, *args, **kwargs):
        # Only the controlled LIVE pilot gets this authority de-duplication.
        if getattr(self, "paper_trade", False) or not bool(
            getattr(getattr(self, "pilot", None), "enabled", False)
        ):
            return await original_open(self, sig, *args, **kwargs)

        token_engine = _ENGINE.set(self)
        token_signal = _SIGNAL.set(sig)
        try:
            return await original_open(self, sig, *args, **kwargs)
        finally:
            _SIGNAL.reset(token_signal)
            _ENGINE.reset(token_engine)

    async def _calculate_with_nexus_authority(
        symbol, direction, closes, highs, lows, volumes, client=None
    ):
        result = await original_calculate(
            symbol, direction, closes, highs, lows, volumes, client
        )
        if not isinstance(result, dict):
            return result

        engine = _ENGINE.get()
        sig = _SIGNAL.get()
        if engine is None or sig is None:
            return result
        if str(getattr(sig, "symbol", "")) != str(symbol):
            return result
        if str(getattr(sig, "direction", "")).upper() != str(direction).upper():
            return result

        decision = _exact_nexus_approval(engine, sig, str(symbol), str(direction))
        if decision is None:
            return result

        legacy_total = _finite(result.get("total"))
        legacy_approved = result.get("aprovado") is True
        # Malformed legacy output is not a reason to bypass anything.
        if legacy_total is None:
            return result

        result["legacy_aprovado"] = legacy_approved
        result["legacy_gate_mode"] = "ADVISORY_AFTER_EXACT_NEXUS_APPROVAL"
        if legacy_approved:
            return result

        result["aprovado"] = True
        log.warning(
            "[LEGACY_PRETRADE_ADVISORY] symbol=%s side=%s legacy_score=%.2f "
            "legacy_min=%s nexus_score=%.2f exact_nexus_execution_allowed=true "
            "result=ADVISORY_CONTINUE hard_gates_unchanged=true",
            symbol,
            direction,
            legacy_total,
            getattr(scoring, "MIN_SCORE", "unknown"),
            float(decision.get("setup_quality", 0.0) or 0.0),
        )
        return result

    TradingEngine._open = _open_with_context
    scoring.calculate = _calculate_with_nexus_authority
    TradingEngine._legacy_pretrade_advisory_installed = True

    log.warning(
        "[LEGACY_PRETRADE_AUTHORITY] installed: legacy score remains telemetry; "
        "only exact NEXUS-approved LIVE pilot trades may continue when legacy<min; "
        "RiskManagerV3/PilotGuard/DD/market-risk/ownership/final-market gates unchanged"
    )
