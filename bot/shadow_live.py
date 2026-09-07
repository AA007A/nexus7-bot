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
from bot.kucoin import TAKER_FEE
from bot.quantity import minimum_base_quantity, validate_base_quantity
from bot.nexus_types import decision_validation_error


async def connect_readonly(engine) -> bool:
    try:
        ping_ok = await engine.client.ping()
        if not ping_ok:
            log.warning("[SHADOW_LIVE] exchange ping failed; continuing with REST read-only checks")
        balance = await engine.client.get_balance()
        if balance < 0:
            engine.connected = False; engine.active = False
            return False
        engine.risk.init(balance); engine.risk.update(balance)
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
            log.warning("[SHADOW_LIVE] %s blocked: not viable", sig.symbol)
            return None

        if getattr(engine, "_durable_state_enforced", False):
            from bot import durable_execution as durable
            from bot import shadow_state_isolation as isolation
            if not durable.can_open(engine):
                hard = isolation.durable_analysis_blocker(engine)
                if hard:
                    log.warning("[SHADOW_LIVE] %s blocked: durable analysis faults=%s", sig.symbol, ",".join(hard))
                    return None
                log.warning("[SHADOW_STATE_ISOLATION] execution state divergent but hypothetical analysis continues; execution_effect=NONE")

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
        log.info("[SHADOW_AI] symbol=%s side=%s decision=%s approved=%s source=%s reason=%s execution_effect=NONE", sig.symbol, sig.direction, "APPROVE" if approved else "REJECT", approved, decision_source, reason or "approved")
        if not approved:
            return None

        if not await engine._refresh_entry_balance():
            return None
        fresh_bal = float(engine.risk.balance or 0)
        if engine.pilot.enabled:
            qty = minimum_base_quantity(engine.instruments[sig.symbol], sig.entry)
            required = qty * sig.entry * (1.0 / cfg.LEVERAGE + TAKER_FEE)
            if fresh_bal <= 0 or required > fresh_bal:
                return None
        else:
            # Existing/manual positions are intentionally excluded only from
            # hypothetical sizing. Real execution gates remain untouched.
            qty = engine.risk.size(sig.symbol, sig.entry, engine.instruments, open_positions={})
        if qty <= 0:
            return None

        kl = engine.client.get_cached_klines(sig.symbol, "15", 50)
        if len(kl) < 20:
            try: kl = await engine.client.get_klines(sig.symbol, "15", 50)
            except Exception: kl = []
        if len(kl) <= 20:
            return None
        c=[float(k.get("c",sig.entry)) for k in kl]; h=[float(k.get("h",sig.entry)) for k in kl]
        l=[float(k.get("l",sig.entry)) for k in kl]; v=[float(k.get("v",1000.0)) for k in kl]
        pre_score = await scoring.calculate(sig.symbol, sig.direction, c, h, l, v, engine.client)
        if not pre_score.get("aprovado"):
            log.info("[SHADOW_LIVE] %s blocked: pretrade=%s minimum=%s execution_effect=NONE", sig.symbol, pre_score.get("total"), scoring.MIN_SCORE)
            return None
        validate_base_quantity(qty, engine.instruments.get(sig.symbol, {}), sig.entry)
        if sig.sl <= 0 or sig.tp <= 0: return None
        if sig.direction == "LONG" and (sig.sl >= sig.entry or sig.tp <= sig.entry): return None
        if sig.direction == "SHORT" and (sig.sl <= sig.entry or sig.tp >= sig.entry): return None
        liq_result = liq.analyze(entry=sig.entry, stop=sig.sl, leverage=cfg.LEVERAGE, is_long=(sig.direction == "LONG"), symbol=sig.symbol, n_open_positions=1)
        if not liq_result.stop_effective:
            return None

        # Pilot evaluation remains informational here. Its blockers are recorded,
        # never bypassed or converted into execution permission.
        try: pilot_blockers = engine.pilot.evaluate(engine, engine.client, sig.symbol, nx_dec)
        except Exception as exc: pilot_blockers = [f"PILOT_EVAL_ERROR:{type(exc).__name__}"]
        record={"symbol":sig.symbol,"side":sig.direction,"entry":float(sig.entry),"sl":float(sig.sl),"tp":float(sig.tp),"qty":float(qty),"notional":float(qty*sig.entry),"score":int(sig.score),"pretrade":int(pre_score.get("total",0)),"ai_approved":True,"pilot_blockers":list(pilot_blockers),"ts":int(time.time()),"execution_effect":"NONE"}
        log.warning("[SHADOW_LIVE] WOULD_SUBMIT symbol=%s side=%s entry=%.8f sl=%.8f tp=%.8f qty=%.12g notional=%.4f score=%s pretrade=%s pilot_blockers=%s execution_effect=NONE", sig.symbol,sig.direction,sig.entry,sig.sl,sig.tp,qty,qty*sig.entry,sig.score,pre_score.get("total",0),len(pilot_blockers))
        return record
    except Exception as exc:
        log.error("[SHADOW_LIVE] %s evaluation failed: %s", getattr(sig,"symbol","?"), type(exc).__name__)
        return None
