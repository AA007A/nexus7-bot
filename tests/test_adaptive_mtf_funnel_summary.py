from collections import Counter

from bot import adaptive_mtf_calibration as calibration


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, message, *args):
        self.lines.append(message % args if args else message)


def _reset_aggregate():
    calibration._AGG_TS = None
    calibration._AGG_FAILURES = Counter()
    calibration._AGG_SAMPLES = 0
    calibration._AGG_NEAR_MISS_1 = 0
    calibration._AGG_NEAR_MISS_2 = 0
    calibration._AGG_EMITTED = False


def _snapshot(failures):
    return {"failures": list(failures), "failure_count": len(failures)}


def test_funnel_summary_emits_after_minimum_independent_samples():
    _reset_aggregate()
    log = _Log()

    samples = [
        ["SCORE_4H", "SCORE_1H", "SCORE_15M", "SCORE_COMBINED"],
        ["SCORE_4H", "SCORE_1H"],
        ["SCORE_4H"],
        ["SCORE_4H", "SCORE_15M", "ALIGN_15M"],
        ["SCORE_4H", "VOLUME"],
        ["SCORE_4H", "ADX", "ALIGN_15M"],
    ]
    for failures in samples:
        calibration._aggregate_snapshot(
            candle_ts=12345, snapshot=_snapshot(failures), log=log
        )

    summaries = [line for line in log.lines if "ADAPTIVE_MTF_FUNNEL_SUMMARY" in line]
    assert len(summaries) == 1
    summary = summaries[0]
    assert "samples=6" in summary
    assert "near_miss_1=1" in summary
    assert "near_miss_2=3" in summary
    assert "multi_gate_gt2=3" in summary
    assert "SCORE_4H:6" in summary
    assert "trigger=sample_threshold" in summary
    assert "decision_effect=NONE" in summary


def test_funnel_summary_rollover_flushes_small_unemitted_bucket():
    _reset_aggregate()
    log = _Log()

    calibration._aggregate_snapshot(
        candle_ts=111, snapshot=_snapshot(["SCORE_4H", "SCORE_1H"]), log=log
    )
    calibration._aggregate_snapshot(
        candle_ts=111, snapshot=_snapshot(["SCORE_4H"]), log=log
    )
    calibration._aggregate_snapshot(
        candle_ts=222, snapshot=_snapshot(["VOLUME"]), log=log
    )

    summaries = [line for line in log.lines if "ADAPTIVE_MTF_FUNNEL_SUMMARY" in line]
    assert len(summaries) == 1
    assert "candle=111" in summaries[0]
    assert "samples=2" in summaries[0]
    assert "trigger=candle_rollover" in summaries[0]


def test_funnel_summary_does_not_change_failure_vector_semantics():
    thresholds = {
        "min_4h": 60,
        "min_1h": 65,
        "min_15m": 80,
        "min_combined": 72,
        "min_vol": 1.2,
        "min_adx": 18,
        "max_extension_atr": 2.5,
    }
    result = calibration.failure_vector(
        direction="SHORT",
        bull_4h=False,
        bear_4h=True,
        bull_1h=False,
        bear_1h=False,
        s4h={"total": 46},
        s1h={"total": 50},
        s15={"total": 62, "aligned": True, "vol_r": 1.9, "adx_v": 25, "rsi_v": 50},
        combined=54,
        entry_type="MOMENTUM",
        extension_atr=0.7,
        thresholds=thresholds,
    )
    assert result["failures"] == ["SCORE_4H", "SCORE_1H", "SCORE_15M", "SCORE_COMBINED"]
