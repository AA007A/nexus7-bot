"""Operator margin-loss geometry, applied before the normal AI entry gate."""
import math


def stop_price(entry, direction, leverage, round_trip_cost):
    values = (entry, leverage, round_trip_cost)
    if not all(math.isfinite(v) for v in values):
        raise ValueError("nonfinite stop input")
    if entry <= 0 or leverage != 50 or round_trip_cost < 0:
        raise ValueError("invalid stop input")
    distance = 0.5 / leverage - round_trip_cost
    if distance <= 0 or direction not in ("LONG", "SHORT"):
        raise ValueError("invalid stop budget")
    return entry * (1 - distance if direction == "LONG" else 1 + distance)


def install(TradingEngine, log):
    if getattr(TradingEngine, "_operator_loss_policy_installed", False):
        return
    from bot.config import cfg
    from bot.kucoin_execution_model import estimated_round_trip_cost_pct
    original_open = TradingEngine._open
    original_exit = TradingEngine._check_stagnation_and_invalidation

    def enabled(engine):
        return not getattr(engine, "paper_trade", True) and bool(
            getattr(getattr(engine, "pilot", None), "enabled", False))

    async def open_with_loss_geometry(self, sig, *args, **kwargs):
        if enabled(self):
            try:
                cost = estimated_round_trip_cost_pct(sig.symbol) / 100.0
                sig.sl = stop_price(float(sig.entry), sig.direction, float(cfg.LEVERAGE), cost)
                distance = abs(float(sig.entry) - sig.sl)
                sig.rr = abs(float(sig.tp) - float(sig.entry)) / distance
                for index in (1, 2):
                    target = getattr(sig, f"tp{index}", None)
                    if target is not None:
                        setattr(sig, f"rr{index}", abs(float(target) - float(sig.entry)) / distance)
                log.info("[OPERATOR_LOSS_POLICY] symbol=%s stop=%.8f margin_loss_target=50%% estimated_cost_pct=%.5f native_execution_may_differ=true", sig.symbol, sig.sl, cost * 100)
            except (ValueError, TypeError, ArithmeticError) as exc:
                log.error("[OPERATOR_LOSS_POLICY] entry blocked: %s", type(exc).__name__)
                return None
        return await original_open(self, sig, *args, **kwargs)

    async def no_discretionary_loss_exit(self):
        if enabled(self):
            return
        return await original_exit(self)

    TradingEngine._open = open_with_loss_geometry
    TradingEngine._check_stagnation_and_invalidation = no_discretionary_loss_exit
    TradingEngine._operator_loss_policy_installed = True
