import inspect

from bot import adaptive_mtf_calibration as cal
from bot import adaptive_mtf_entry


def _thresholds():
    return {
        "min_combined": 72,
        "min_4h": 60,
        "min_1h": 65,
        "min_15m": 80,
        "min_vol": 1.20,
        "min_adx": 18,
        "max_extension_atr": 2.50,
    }


def test_failure_vector_reports_all_failed_thresholds_not_only_first():
    snap = cal.failure_vector(
        direction="SHORT",
        bull_4h=False, bear_4h=False,
        bull_1h=False, bear_1h=True,
        s4h={"ok": True, "total": 53},
        s1h={"ok": True, "total": 58},
        s15={"ok": True, "total": 69, "aligned": True,
             "vol_r": 1.35, "adx_v": 22, "rsi_v": 40},
        combined=62,
        entry_type="MOMENTUM",
        extension_atr=1.2,
        thresholds=_thresholds(),
    )
    assert snap["failures"] == [
        "SCORE_4H", "SCORE_1H", "SCORE_15M", "SCORE_COMBINED"
    ]
    assert snap["failure_count"] == 4
    assert snap["gap_4h"] == 7
    assert snap["gap_1h"] == 7
    assert snap["gap_15m"] == 11
    assert snap["gap_combined"] == 10
    assert snap["gap_volume"] == 0
    assert snap["gap_adx"] == 0


def test_failure_vector_captures_non_score_requirements_together():
    snap = cal.failure_vector(
        direction="LONG",
        bull_4h=False, bear_4h=False,
        bull_1h=True, bear_1h=False,
        s4h={"ok": True, "total": 70},
        s1h={"ok": True, "total": 70},
        s15={"ok": True, "total": 85, "aligned": False,
             "vol_r": 0.9, "adx_v": 12, "rsi_v": 65},
        combined=78,
        entry_type="PULLBACK",
        extension_atr=3.1,
        thresholds=_thresholds(),
    )
    assert snap["failures"] == [
        "ALIGN_15M", "ENTRY_TYPE", "VOLUME", "ADX", "EXTENSION"
    ]
    assert snap["gap_volume"] == 0.3
    assert snap["gap_adx"] == 6
    assert snap["extension_excess"] == 0.6


def test_calibration_module_has_no_execution_or_threshold_mutation():
    source = inspect.getsource(cal)
    forbidden = (
        "place_order",
        "create_order",
        "cancel_order",
        "close_position",
        "_open(",
        "cfg.LEVERAGE =",
        "ADAPTIVE_MTF_MIN_SCORE =",
        "ADAPTIVE_MTF_MIN_4H_SCORE =",
        "ADAPTIVE_MTF_MIN_1H_SCORE =",
        "ADAPTIVE_MTF_MIN_15M_SCORE =",
    )
    assert all(token not in source for token in forbidden)
    assert "decision_effect=NONE execution_effect=NONE" in source
    assert "leverage_unchanged=true" in source


def test_adaptive_runtime_only_observes_after_gate_reject():
    source = inspect.getsource(adaptive_mtf_entry)
    assert "adaptive_mtf_calibration.observe_reject" in source
    assert source.index("allowed, reason = _gate(") < source.index(
        "adaptive_mtf_calibration.observe_reject"
    )
    assert source.index("adaptive_mtf_calibration.observe_reject") < source.index(
        'log.debug("[ADAPTIVE_MTF] symbol=%s result=HOLD reason=%s"'
    )


def test_closed_candle_dedupe_key_uses_previous_bar_when_history_present():
    bars = [{"ts": i} for i in range(25)]
    assert cal._closed_15m_ts(bars) == 23
