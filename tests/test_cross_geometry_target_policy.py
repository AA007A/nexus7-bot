"""Regressions for preserving strategy TP under 50x stop compression."""
from __future__ import annotations

import inspect
from types import SimpleNamespace

from bot import cross_geometry_target_policy as policy
from bot import runtime_overlays


class _Log:
    def __init__(self):
        self.messages = []

    def warning(self, message, *args):
        self.messages.append(message % args if args else message)


def _sig(*, direction="SHORT", entry=100.0, sl=101.402, tp=97.196):
    return SimpleNamespace(
        symbol="LINKUSDT",
        direction=direction,
        entry=entry,
        sl=sl,
        tp=tp,
    )


def _net_rr(entry, sl, tp, *, taker=0.0006, slippage=0.0002434):
    reward = abs(tp - entry) / entry
    risk = abs(entry - sl) / entry
    round_trip_cost = 2.0 * (taker + slippage)
    return (reward - round_trip_cost) / (risk + round_trip_cost)


def test_production_like_link_case_preserves_original_target_and_net_rr():
    sig = _sig()
    # Production observation: original stop ~1.402%, liquidation-safe stop
    # ~0.861%. Legacy proportional compression moved a 2R target to 1.722%.
    base = {
        "status": "ADJUSTED",
        "reason": "compressed_for_configured_leverage",
        "sl": 100.861,
        "tp": 98.278,
        "rr": 2.0,
        "original_stop_pct": 1.402,
        "final_stop_pct": 0.861,
        "retained_fraction": 0.614,
    }

    revised = policy.apply_target_policy(base, sig)

    assert revised["status"] == "ADJUSTED"
    assert revised["sl"] == base["sl"]
    assert revised["tp"] == sig.tp
    assert revised["tp"] != base["tp"]
    assert revised["rr"] > 3.0
    assert revised["original_target_preserved"] is True
    assert revised["target_policy"] == "PRESERVE_ORIGINAL_STRATEGY_TARGET"

    legacy_net_rr = _net_rr(sig.entry, base["sl"], base["tp"])
    revised_net_rr = _net_rr(sig.entry, revised["sl"], revised["tp"])
    assert legacy_net_rr < 1.60
    assert revised_net_rr > 1.60


def test_long_side_is_symmetric_and_never_extends_target():
    sig = _sig(direction="LONG", sl=98.598, tp=102.804)
    base = {
        "status": "ADJUSTED",
        "sl": 99.139,
        "tp": 101.722,
        "rr": 2.0,
    }
    revised = policy.apply_target_policy(base, sig)
    assert revised["sl"] == 99.139
    assert revised["tp"] == 102.804
    assert revised["rr"] > 3.0
    # Preserving the original target is not target extension: it exactly
    # restores the strategy-requested level and never goes beyond it.
    assert revised["tp"] == sig.tp


def test_non_adjusted_geometry_is_untouched():
    sig = _sig()
    safe = {"status": "SAFE", "sl": sig.sl, "tp": sig.tp, "rr": 2.0}
    assert policy.apply_target_policy(safe, sig) is safe


def test_malformed_adjusted_geometry_fails_closed():
    sig = _sig()
    bad = {"status": "ADJUSTED", "sl": 99.0, "tp": 98.0, "rr": 2.0}
    result = policy.apply_target_policy(bad, sig)
    assert result["status"] == "BLOCK"
    assert "target_policy_invalid_short_geometry" in result["reason"]


def test_install_wraps_only_module_geometry_helper_and_is_idempotent():
    calls = []

    def original(liquidation, sig, leverage):
        calls.append(leverage)
        return {"status": "ADJUSTED", "sl": 100.861, "tp": 98.278, "rr": 2.0}

    fake = SimpleNamespace(_geometry_from_exact_mmr=original)
    log = _Log()
    policy.install(fake, log)
    first_wrapper = fake._geometry_from_exact_mmr
    policy.install(fake, log)
    assert fake._geometry_from_exact_mmr is first_wrapper

    result = fake._geometry_from_exact_mmr(None, _sig(), 50)
    assert calls == [50]
    assert result["tp"] == 97.196
    assert result["rr"] > 3.0
    assert any("CROSS_GEOMETRY_TARGET_POLICY" in message for message in log.messages)


def test_policy_cannot_change_leverage_thresholds_sizing_or_order_routing():
    source = inspect.getsource(policy)
    overlay_source = inspect.getsource(runtime_overlays.install)

    assert "cross_target_policy.install(cross_risk_hardening, log)" in overlay_source
    assert "cfg.LEVERAGE" not in source
    assert "LEVERAGE =" not in source
    assert "NEXUS_MIN_RR_NET" not in source
    assert "NEXUS_MIN_SCORE" not in source
    assert "place_order" not in source
    assert "create_order" not in source
    assert "RiskManager" not in source
