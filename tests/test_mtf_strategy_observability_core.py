from pathlib import Path
from unittest.mock import patch

from bot import mtf_shadow
from bot.mtf_strategy_observability import observe_analyze_mtf


ROOT = Path(__file__).resolve().parents[1]


class _Dummy:
    pass


def _call_args():
    k15 = [{"c": 1.0}]
    k1h = [{"c": 2.0}]
    k4h = [{"c": 3.0}]
    return k15, k1h, k4h


def test_decorator_returns_exact_production_result_and_observes_it():
    marker = object()

    @observe_analyze_mtf
    def original(self, symbol, k15, k1h, k4h,
                 min_score=60, fee_mult=2.0, vol_mult=1.0):
        return marker

    k15, k1h, k4h = _call_args()
    with (
        patch.object(
            mtf_shadow,
            "snapshot",
            side_effect=[
                {"unique_states": 0},
                {
                    "unique_states": 1,
                    "eligible_4h_dir_1h_neutral": 0,
                    "shadow_pre_ai_survivors": 0,
                    "shadow_nexus_approved": 0,
                    "shadow_nexus_vetoed": 0,
                },
            ],
        ),
        patch.object(mtf_shadow, "observe") as observe,
    ):
        result = original(
            _Dummy(), "BTCUSDT", k15, k1h, k4h,
            min_score=67, fee_mult=2.5, vol_mult=1.2,
        )

    assert result is marker
    observe.assert_called_once_with(
        "BTCUSDT", k15, k1h, k4h,
        production_result=marker,
        min_score=67,
        fee_mult=2.5,
        vol_mult=1.2,
    )


def test_observability_failure_cannot_change_production_result():
    marker = object()

    @observe_analyze_mtf
    def original(self, symbol, k15, k1h, k4h,
                 min_score=60, fee_mult=2.0, vol_mult=1.0):
        return marker

    k15, k1h, k4h = _call_args()
    with (
        patch.object(mtf_shadow, "snapshot", return_value={"unique_states": 0}),
        patch.object(mtf_shadow, "observe", side_effect=RuntimeError("telemetry down")),
    ):
        result = original(_Dummy(), "ETHUSDT", k15, k1h, k4h)

    assert result is marker


def test_strategy_owns_mtf_observability_declaratively():
    strategy = (ROOT / "bot" / "strategy.py").read_text(encoding="utf-8")
    overlays = (ROOT / "bot" / "runtime_overlays.py").read_text(encoding="utf-8")

    assert "from bot.mtf_strategy_observability import observe_analyze_mtf" in strategy
    assert "@observe_analyze_mtf" in strategy
    assert "Analyzer.analyze_mtf =" not in overlays
    assert "_mtf_shadow_patched" not in overlays


def test_mtf_observability_helper_has_no_execution_or_exchange_mutation():
    text = (ROOT / "bot" / "mtf_strategy_observability.py").read_text(encoding="utf-8")
    for marker in (
        "place_order(",
        "cancel_order(",
        "cancel_all_orders(",
        "set_leverage(",
        "set_position_stops(",
        "close_position(",
        "PILOT_RELEASE_APPROVED=",
        "LIVE_TRADING_CONFIRMED=",
        "PAPER_TRADE=false",
    ):
        assert marker not in text
