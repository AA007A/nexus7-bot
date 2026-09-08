from pathlib import Path
from unittest.mock import patch

from bot import mtf_shadow
from bot.mtf_shadow_observability import observe_result


ROOT = Path(__file__).resolve().parents[1]


def test_observe_result_preserves_identity_and_arguments():
    sentinel = object()
    seen = {}

    def fake_observe(symbol, k15, k1h, k4h, **kwargs):
        seen["symbol"] = symbol
        seen["k15"] = k15
        seen["k1h"] = k1h
        seen["k4h"] = k4h
        seen.update(kwargs)

    with (
        patch.object(mtf_shadow, "snapshot", side_effect=[{"unique_states": 0}, {"unique_states": 0}]),
        patch.object(mtf_shadow, "observe", side_effect=fake_observe),
    ):
        out = observe_result(
            "BTCUSDT",
            ["15"],
            ["1h"],
            ["4h"],
            sentinel,
            min_score=65,
            fee_mult=2.5,
            vol_mult=1.2,
        )

    assert out is sentinel
    assert seen["symbol"] == "BTCUSDT"
    assert seen["production_result"] is sentinel
    assert seen["min_score"] == 65
    assert seen["fee_mult"] == 2.5
    assert seen["vol_mult"] == 1.2


def test_observe_result_is_best_effort_and_still_preserves_result():
    sentinel = object()
    with patch.object(mtf_shadow, "snapshot", side_effect=RuntimeError("telemetry down")):
        out = observe_result("ETHUSDT", [], [], [], sentinel)
    assert out is sentinel


def test_runtime_overlay_only_delegates_mtf_telemetry():
    text = (ROOT / "bot" / "runtime_overlays.py").read_text(encoding="utf-8")
    assert "mtf_shadow_observability.observe_result(" in text
    assert "mtf_shadow.observe(" not in text
    assert "ms.observe(" not in text


def test_helper_has_no_execution_or_strategy_mutation_paths():
    text = (ROOT / "bot" / "mtf_shadow_observability.py").read_text(encoding="utf-8")
    for marker in (
        "place_order(",
        "cancel_order(",
        "set_position_stops(",
        "set_leverage(",
        "execution_allowed =",
        "min_score =",
        "PILOT_RELEASE_APPROVED=",
        "LIVE_TRADING_CONFIRMED=",
    ):
        assert marker not in text
