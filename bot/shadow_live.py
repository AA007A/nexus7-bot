"""Read-only SHADOW LIVE execution path for NEXUS-7.

Purpose: exercise live market data, NEXUS AI, risk sizing, pre-trade scoring,
and liquidation checks while guaranteeing execution_effect=NONE.

This module never submits/cancels orders, changes leverage, sets stops, or starts
private order streams. It is only used by validation_safety_lock while LIVE is
safety-held.
"""
import asyncio
import time

from bot.config import cfg
from bot.logger import log
from bot import score as scoring
from bot import liquidation as liq
from bot import shadow_balance_semantics as balance_semantics
from bot.kucoin import TAKER_FEE
from bot.quantity import minimum_base_quantity, validate_base_quantity
from bot.nexus_types import decision_validation_error


def _shadow_ai_reason(nx_dec, validation_reason, approved):
    """Return an accurate, bounded observability reason without changing decisions."""
    if approved:
        return "approved"
    if validation_reason:
        return str(validation_reason)
    if nx_dec is None:
        return "no_decision"
    reasoning = getattr(nx_dec, "reasoning", None)
    if isinstance(reasoning, list):
        for item in reasoning:
            if isinstance(item, str) and item.strip():
                return item.strip()[:240]
    warnings = getattr(nx_dec, "warnings", None)
    if isinstance(warnings, list):
        for item in warnings:
            if isinstance(item, str) and item.strip():
                return item.strip()[:240]
    return "nexus_ai_veto"


def _gate_block(symbol, stage, reason, **fields):
    """Emit bounded, read-only observability for a SHADOW gate rejection."""
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    log.info(
        "[SHADOW_GATE] symbol=%s stage=%s result=BLOCK reason=%s%s execution_effect=NONE",
        symbol,
        stage,
        reason,
        f" {extra}" if extra else "",
    )


def _gate_pass(symbol, stage, **fields):
    """Emit bounded, read-only observability for a SHADOW gate pass."""
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    log.info(
        "[SHADOW_GATE] symbol=%s stage=%s result=PASS%s execution_effect=NONE",
        symbol,
        stage,
        f" {extra}" if extra else "",
    )


async def connect_readonly(engine) -> bool:
    try:
        ping_ok = await engine.client.ping()
        if not ping_ok:
            log.warning("[SHADOW_LIVE] exchange ping failed; continuing with REST read-only checks")
        state = await balance_semantics.refresh_shadow_risk(engine)
        if state["equity"] < 0 or state["available"] < 0:
            engine.connected = False; engine.active = False
            return False
        await engine.client.load_instruments()
        engine.instruments = engine.client.get_instruments()
        await engine._filter_viable_symbols()
        if not engine.viable_symbols:
            engine.connected = False; engine.active = False
            return False
        try:
            await asyncio.wait_for(engine.client.start_websocket(engine.viable_symbols[:30], intervals=["15", "60", "240"]), timeout=10)
        except asyncio.TimeoutError:
            log.warning("[SHADOW_LIVE] public websocket startup timeout; REST fallback remains available")
        except Exception as exc:
            log.warning("[SHADOW_LIVE] public websocket unavailable: %s", type(exc).__name__)
        engine.connected = True; engine.active = True
        log.warning("[SHADOW_LIVE] READY balance_read=true viable_symbols=%s private_ws=false execution_effect=NONE", len(engine.viable_symbols))
        return True
    except Exception as exc:
        engine.connected = False; engine.active = False
        log.error("[SHADOW_LIVE] connect failed: %s", type(exc).__name__)
        return False


async def evaluate_candidate(engine, sig):
    """Evaluate a real-market candidate without granting execution authority."""
    try:
        if sig.symbol not in engine.viable_symbols:
            _gate_block(sig.symbol, "VIABILITY", "not_viable")
            return None
        _gate_pass(sig.symbol, "VIABILITY")

        if getattr(engine, "_durable_state_enforced", False):
            from bot import durable_execution as durable
            from bot import shadow_state_isolation as isolation
            if not durable.can_open(engine):
                hard = isolation.durable_analysis_blocker(engine)
                if hard:
                    _gate_block(sig.symbol, "DURABLE_STATE", "hard_fault", faults=",".join(hard))
                    return None
                log.warning("[SHADOW_STATE_ISOLATION] execution state divergent but hypothetical analysis continues; execution_effect=NONE")
        _gate_pass(sig.symbol, "DURABLE_STATE")

        from bot import engine as engine_mod
        nx_dec = None; reason = "ai_disabled"; decision_source = "validation_failure"
        if getattr(engine_mod, "_NEXUS_ENABLED", True):
            try:
                nx_dec = await asyncio.wait_for(engine._nexus_validate(sig), timeout=float(getattr(engine_mod, "_NEXUS_TIMEOUT_S", 10.0)))
                reason = decision_validation_error(nx_dec, sig.symbol, sig.direction, sig.entry, sig.sl, sig.tp)
                decision_source = "validation_failure" if reason else "nexus_ai"
            except asyncio.TimeoutError:
                decision_source, reason = "timeout", "ai_timeout"
            except Exception as exc:
                decision_source, reason = "exception", type(exc).__name__
        approved = bool(reason is None and nx_dec is not None and nx_dec.execution_allowed is True)
        observed_reason = _shadow_ai_reason(nx_dec, reason, approved)
        log.info("[SHADOW_AI] symbol=%s side=%s decision=%s approved=%s source=%s reason=%s execution_effect=NONE", sig.symbol, sig.direction, "APPROVE" if approved else "REJECT", approved, decision_source, observed_reason)
        if not approved:
            _gate_block(sig.symbol, "NEXUS_AI", observed_reason, source=decision_source)
            return None
        _gate_pass(sig.symbol, "NEXUS_AI", source=decision_source)

        state = await balance_semantics.refresh_shadow_risk(engine)
        equity = float(state["equity"])
        available = float(state["available"])
        if equity <= 0:
            _gate_block(sig.symbol, "CAPITAL_HEALTH", "non_positive_equity", equity=equity)
            return None
        if not getattr(engine.risk, "balance_confirmed", True):
            _gate_block(sig.symbol, "CAPITAL_HEALTH", "unconfirmed_equity", equity=equity)
            return None
        if engine.risk.drawdown >= cfg.MAX_DRAWDOWN:
            _gate_block(
                sig.symbol,
                "CAPITAL_HEALTH",
                "drawdown_limit",
                drawdown=f"{engine.risk.drawdown:.6f}",
                maximum=f"{cfg.MAX_DRAWDOWN:.6f}",
            )
            return None
        _gate_pass(sig.symbol, "CAPITAL_HEALTH", equity=f"{equity:.4f}", drawdown=f"{engine.risk.drawdown:.6f}")

        if engine.pilot.enabled:
            qty = minimum_base_quantity(engine.instruments[sig.symbol], sig.entry)
            sizing_mode = "pilot_minimum"
        else:
            # Existing/manual positions are intentionally excluded only from
            # hypothetical sizing. Real execution gates remain untouched.
            qty = engine.risk.size(sig.symbol, sig.entry, engine.instruments, open_positions={})
            sizing_mode = "risk_manager"
        if qty <= 0:
            _gate_block(sig.symbol, "SIZING", "non_positive_qty", mode=sizing_mode)
            return None
        _gate_pass(sig.symbol, "SIZING", mode=sizing_mode, qty=f"{qty:.12g}")

        collateral_ok, required = balance_semantics.collateral_allows(
            qty, sig.entry, available, cfg.LEVERAGE, TAKER_FEE
        )
        if not collateral_ok:
            _gate_block(
                sig.symbol,
                "COLLATERAL",
                "insufficient_available_balance",
                required=f"{required:.4f}",
                available=f"{available:.4f}",
                equity=f"{equity:.4f}",
            )
            return None
        _gate_pass(sig.symbol, "COLLATERAL", required=f"{required:.4f}", available=f"{available:.4f}")

        kl = engine.client.get_cached_klines(sig.symbol, "15", 50)
        if len(kl) < 20:
            try:
                kl = await engine.client.get_klines(sig.symbol, "15", 50)
            except Exception as exc:
                _gate_block(sig.symbol, "PRETRADE_DATA", "kline_fetch_error", error=type(exc).__name__)
                kl = []
        if len(kl) <= 20:
            _gate_block(sig.symbol, "PRETRADE_DATA", "insufficient_klines", count=len(kl), minimum=21)
            return None
        _gate_pass(sig.symbol, "PRETRADE_DATA", count=len(kl))

        c=[float(k.get("c",sig.entry)) for k in kl]; h=[float(k.get("h",sig.entry)) for k in kl]
        l=[float(k.get("l",sig.entry)) for k in kl]; v=[float(k.get("v",1000.0)) for k in kl]
        pre_score = await scoring.calculate(sig.symbol, sig.direction, c, h, l, v, engine.client)
        if not pre_score.get("aprovado"):
            _gate_block(
                sig.symbol,
                "PRETRADE_SCORE",
                "score_below_gate",
                score=pre_score.get("total"),
                minimum=scoring.MIN_SCORE,
            )
            return None
        _gate_pass(sig.symbol, "PRETRADE_SCORE", score=pre_score.get("total"), minimum=scoring.MIN_SCORE)

        try:
            validate_base_quantity(qty, engine.instruments.get(sig.symbol, {}), sig.entry)
        except Exception as exc:
            _gate_block(sig.symbol, "QUANTITY_VALIDATION", type(exc).__name__)
            return None
        _gate_pass(sig.symbol, "QUANTITY_VALIDATION", qty=f"{qty:.12g}")

        if sig.sl <= 0 or sig.tp <= 0:
            _gate_block(sig.symbol, "PROTECTIVE_LEVELS", "non_positive_sl_or_tp", sl=sig.sl, tp=sig.tp)
            return None
        if sig.direction == "LONG" and (sig.sl >= sig.entry or sig.tp <= sig.entry):
            _gate_block(sig.symbol, "PROTECTIVE_LEVELS", "invalid_long_geometry", entry=sig.entry, sl=sig.sl, tp=sig.tp)
            return None
        if sig.direction == "SHORT" and (sig.sl <= sig.entry or sig.tp >= sig.entry):
            _gate_block(sig.symbol, "PROTECTIVE_LEVELS", "invalid_short_geometry", entry=sig.entry, sl=sig.sl, tp=sig.tp)
            return None
        _gate_pass(sig.symbol, "PROTECTIVE_LEVELS")

        liq_result = liq.analyze(entry=sig.entry, stop=sig.sl, leverage=cfg.LEVERAGE, is_long=(sig.direction == "LONG"), symbol=sig.symbol, n_open_positions=1)
        if not liq_result.stop_effective:
            _gate_block(sig.symbol, "LIQUIDATION_GUARD", "stop_not_effective")
            return None
        _gate_pass(sig.symbol, "LIQUIDATION_GUARD")

        # Pilot evaluation remains informational here. Its blockers are recorded,
        # never bypassed or converted into execution permission.
        try:
            pilot_blockers = engine.pilot.evaluate(engine, engine.client, sig.symbol, nx_dec)
        except Exception as exc:
            pilot_blockers = [f"PILOT_EVAL_ERROR:{type(exc).__name__}"]
        _gate_pass(sig.symbol, "PILOT_OBSERVABILITY", blockers=len(pilot_blockers))

        record={"symbol":sig.symbol,"side":sig.direction,"entry":float(sig.entry),"sl":float(sig.sl),"tp":float(sig.tp),"qty":float(qty),"notional":float(qty*sig.entry),"score":int(sig.score),"pretrade":int(pre_score.get("total",0)),"ai_approved":True,"pilot_blockers":list(pilot_blockers),"ts":int(time.time()),"execution_effect":"NONE"}
        log.warning("[SHADOW_LIVE] WOULD_SUBMIT symbol=%s side=%s entry=%.8f sl=%.8f tp=%.8f qty=%.12g notional=%.4f score=%s pretrade=%s pilot_blockers=%s execution_effect=NONE", sig.symbol,sig.direction,sig.entry,sig.sl,sig.tp,qty,qty*sig.entry,sig.score,pre_score.get("total",0),len(pilot_blockers))
        return record
    except Exception as exc:
        log.error("[SHADOW_LIVE] %s evaluation failed: %s", getattr(sig,"symbol","?"), type(exc).__name__)
        return None
