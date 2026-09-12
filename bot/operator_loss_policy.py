"""Operator margin-loss geometry, applied before the normal AI entry gate."""
import math
import os


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


def required_target_price(entry, stop, direction, round_trip_cost, min_rr_net):
    """Return the nearest target whose estimated net R:R meets ``min_rr_net``.

    NEXUS evaluates net R:R as (target_distance - cost) /
    (stop_distance + cost).  Reusing a technical TP after widening the operator
    loss stop can therefore collapse a previously healthy gross R:R.  Derive
    the minimum target from the same economics instead of weakening NEXUS.
    """
    values = (entry, stop, round_trip_cost, min_rr_net)
    if not all(math.isfinite(v) for v in values):
        raise ValueError("nonfinite target input")
    if entry <= 0 or stop <= 0 or round_trip_cost < 0 or min_rr_net <= 0:
        raise ValueError("invalid target input")
    if direction not in ("LONG", "SHORT"):
        raise ValueError("invalid target direction")

    stop_distance = abs(entry - stop) / entry
    target_distance = min_rr_net * (stop_distance + round_trip_cost) + round_trip_cost
    if not math.isfinite(target_distance) or target_distance <= 0:
        raise ValueError("invalid target distance")
    return entry * (1 + target_distance if direction == "LONG" else 1 - target_distance)


def _farther_target(current, required, entry, direction):
    current = float(current or 0.0)
    if direction == "LONG":
        return max(current, required) if current > entry else required
    return min(current, required) if 0 < current < entry else required


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
                min_rr_net = float(os.environ.get(
                    "NEXUS_MIN_RR_NET",
                    str(round(float(cfg.MIN_RR_RATIO) * 0.80, 2)),
                ))
                sig.sl = stop_price(float(sig.entry), sig.direction, float(cfg.LEVERAGE), cost)
                required_tp = required_target_price(
                    float(sig.entry), float(sig.sl), sig.direction, cost, min_rr_net
                )
                old_tp = float(sig.tp)
                sig.tp = _farther_target(old_tp, required_tp, float(sig.entry), sig.direction)

                # Preserve a nearer technical TP1 for partial profit-taking, but
                # ensure the final TP2 is not weaker than the execution target.
                if hasattr(sig, "tp2"):
                    sig.tp2 = _farther_target(
                        getattr(sig, "tp2", 0.0), sig.tp, float(sig.entry), sig.direction
                    )

                distance = abs(float(sig.entry) - sig.sl)
                sig.rr = abs(float(sig.tp) - float(sig.entry)) / distance
                for index in (1, 2):
                    target = getattr(sig, f"tp{index}", None)
                    if target is not None:
                        setattr(sig, f"rr{index}", abs(float(target) - float(sig.entry)) / distance)

                target_distance = abs(float(sig.tp) - float(sig.entry)) / float(sig.entry)
                stop_distance = distance / float(sig.entry)
                net_rr_est = (target_distance - cost) / (stop_distance + cost)
                log.info(
                    "[OPERATOR_LOSS_POLICY] symbol=%s stop=%.8f tp_before=%.8f tp_final=%.8f "
                    "margin_loss_target=50%% min_rr_net=%.3f estimated_net_rr=%.3f "
                    "estimated_cost_pct=%.5f target_adjusted=%s native_execution_may_differ=true",
                    sig.symbol, sig.sl, old_tp, sig.tp, min_rr_net, net_rr_est,
                    cost * 100, str(abs(sig.tp - old_tp) > 1e-12).lower(),
                )
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
