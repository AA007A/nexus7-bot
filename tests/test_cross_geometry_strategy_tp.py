"""Regression tests for strategy-TP preservation after CROSS SL compression."""
from types import SimpleNamespace

from bot import cross_geometry_strategy_tp as overlay


class _Log:
    def __init__(self): self.lines = []
    def warning(self, msg, *args): self.lines.append(msg % args if args else msg)


def _module(result):
    return SimpleNamespace(_geometry_from_exact_mmr=lambda liquidation, sig, leverage: dict(result))


def _sig(direction="SHORT", entry=100.0, sl=101.4, tp=97.2):
    return SimpleNamespace(symbol="LINKUSDT", direction=direction, entry=entry, sl=sl, tp=tp)


def test_adjusted_short_preserves_original_strategy_tp_and_recomputes_rr():
    module = _module({
        "status": "ADJUSTED", "sl": 100.86, "tp": 98.28, "rr": 2.0,
        "reason": "compressed_for_configured_leverage",
    })
    log = _Log(); overlay.install(module, log)
    out = module._geometry_from_exact_mmr(None, _sig(), 50)
    assert out["sl"] == 100.86
    assert out["tp"] == 97.2
    assert out["strategy_tp_preserved"] is True
    assert out["rr"] > 3.2
    assert out["compressed_tp_legacy"] == 98.28
    assert any("nexus_recheck_still_required=true" in line for line in log.lines)


def test_adjusted_long_is_symmetric():
    module = _module({"status": "ADJUSTED", "sl": 99.1, "tp": 101.8, "rr": 2.0})
    overlay.install(module, _Log())
    out = module._geometry_from_exact_mmr(None, _sig("LONG", 100.0, 98.5, 103.0), 50)
    assert out["sl"] == 99.1
    assert out["tp"] == 103.0
    assert out["rr"] > 3.3


def test_safe_and_block_results_are_untouched():
    for status in ("SAFE", "BLOCK"):
        original = {"status": status, "sl": 101.0, "tp": 98.0, "rr": 2.0}
        module = _module(original); overlay.install(module, _Log())
        assert module._geometry_from_exact_mmr(None, _sig(), 50) == original


def test_invalid_original_tp_fails_closed_to_existing_adjusted_geometry():
    original = {"status": "ADJUSTED", "sl": 100.86, "tp": 98.28, "rr": 2.0}
    module = _module(original); overlay.install(module, _Log())
    # SHORT TP above entry is invalid, so overlay must not substitute it.
    out = module._geometry_from_exact_mmr(None, _sig("SHORT", 100.0, 101.4, 100.5), 50)
    assert out == original


def test_overlay_does_not_expose_threshold_leverage_or_order_mutation():
    import inspect
    source = inspect.getsource(overlay)
    assert "NEXUS_MIN_RR_NET" not in source
    assert "NEXUS_MIN_SCORE" not in source
    assert "cfg.LEVERAGE" not in source
    assert "place_order" not in source
    assert "create_order" not in source
