from __future__ import annotations

import inspect
from collections import Counter

import numpy as np

from bot import scan_summary_hardening as scan_diag
from bot import strategy
from bot import volume_gate_shadow as shadow


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, message, *args):
        self.lines.append(message % args if args else message)

    def debug(self, message, *args):
        self.lines.append(message % args if args else message)


def _reset_shadow_state():
    with shadow._LOCK:
        shadow._SEEN.clear()
        shadow._SEEN_ORDER.clear()
        shadow._ACTIVE.clear()
        shadow._METRICS["unique"] = 0
        shadow._METRICS["eligible"] = 0
        shadow._METRICS["resolved"] = 0
        shadow._METRICS["outcomes"] = Counter()
        for bucket in shadow._METRICS["thresholds"].values():
            bucket.update({"eligible": 0, "resolved": 0, "tp": 0, "sl": 0,
                           "timeout": 0, "net_sum": 0.0})


def _bars(n, start=1_700_000_000_000):
    out = []
    for i in range(n):
        px = 100.0 + i * 0.01
        out.append({
            "ts": start + i * 900_000,
            "o": px - 0.02,
            "h": px + 0.10,
            "l": px - 0.10,
            "c": px,
            "v": 1000.0,
        })
    return out


def test_volume_shadow_registers_only_sub_040_candidate_that_otherwise_survives(monkeypatch):
    _reset_shadow_state()
    k15, k1h, k4h = _bars(30), _bars(20), _bars(15)

    monkeypatch.setattr(strategy, "detect_regime", lambda *a, **k: "TRENDING_UP")
    monkeypatch.setattr(strategy, "ema", lambda closes, period: np.array([99.0 if period == 20 else 98.0]))
    monkeypatch.setattr(strategy, "atr", lambda h, l, c: np.array([1.0] * len(c)))
    monkeypatch.setattr(
        strategy,
        "score_tf",
        lambda *a, **k: {"ok": True, "total": 70, "rsi_v": 50.0,
                         "vol_r": 0.35, "aligned": True},
    )
    monkeypatch.setattr(strategy, "detect_entry", lambda *a, **k: (True, "PULLBACK"))

    shadow.observe(
        "TESTUSDT", k15, k1h, k4h,
        production_result=None, min_score=60, fee_mult=2.0, log=_Log(),
    )
    snap = shadow.snapshot()

    assert snap["eligible"] == 1
    assert snap["active"] == 1
    assert snap["thresholds"]["0.25"]["eligible"] == 1
    assert snap["thresholds"]["0.3"]["eligible"] == 1
    assert snap["thresholds"]["0.35"]["eligible"] == 1
    assert snap["thresholds"]["0.4"]["eligible"] == 0


def test_volume_shadow_same_bar_tp_sl_is_conservative_stop_first():
    _reset_shadow_state()
    sid = "state1"
    with shadow._LOCK:
        shadow._ACTIVE[sid] = {
            "symbol": "TESTUSDT", "direction": "LONG", "entry": 100.0,
            "sl": 99.0, "tp": 102.0, "volume_ratio_15m": 0.35,
            "thresholds_that_would_admit": [0.25, 0.30, 0.35],
            "opened_bar_ts": 1, "last_bar_ts": 1, "bars": 0,
            "mfe_pct": 0.0, "mae_pct": 0.0,
        }
    bar = {"ts": 2, "o": 100.0, "h": 103.0, "l": 98.0, "c": 101.0, "v": 1.0}
    shadow._update_outcomes("TESTUSDT", [bar], _Log())
    snap = shadow.snapshot()

    assert snap["active"] == 0
    assert snap["resolved"] == 1
    assert snap["outcomes"]["AMBIGUOUS_STOP_FIRST"] == 1
    assert snap["thresholds"]["0.35"]["sl"] == 1


def test_volume_shadow_has_no_execution_or_threshold_mutation():
    source = inspect.getsource(shadow)
    forbidden = (
        "place_order", "create_order", "_nexus_validate", "nexus_ai.decide",
        "NEXUS_MIN_RR_NET", "NEXUS_MIN_SCORE", "MIN_ENTRY_SCORE =",
        "MIN_VOLUME_MULT =", "cfg.LEVERAGE =",
    )
    assert all(token not in source for token in forbidden)
    assert shadow._THRESHOLDS == (0.25, 0.30, 0.35, 0.40)


def test_legacy_forming_candle_diagnostic_is_labeled_as_noncanonical():
    raw = (
        "[BTCUSDT] Score=50/100 (4H:61 1H:66 15M:51) "
        "| regime=TRENDING_DOWN RSI=44 vol=0.81x | 4H=↓ 1H=↓ → HOLD"
    )
    rewritten = scan_diag._rewrite_hold_diagnostic(raw)
    assert rewritten.startswith("[LEGACY_FORMING_CANDLE_DIAG] [BTCUSDT]")
    assert "execution_effect=NONE" in rewritten
    assert "Score=58/100" in rewritten
