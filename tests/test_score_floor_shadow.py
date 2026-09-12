import asyncio
import inspect
from collections import Counter

import numpy as np

from bot import score_floor_shadow as shadow
from bot import score_floor_shadow_persistence as persistence
from bot import strategy
from bot.nexus_types import NexusDecision


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, message, *args):
        self.lines.append(message % args if args else message)

    def debug(self, message, *args):
        self.lines.append(message % args if args else message)


def _bars(n=80, last=100.0):
    return [
        {"ts": i, "o": last + 0.1, "h": last + 0.5, "l": last - 0.5,
         "c": last, "v": 1000.0}
        for i in range(1, n + 1)
    ]


def _reset():
    with shadow._LOCK:
        shadow._SEEN.clear()
        shadow._ACTIVE.clear()
        shadow._METRICS["unique"] = 0
        shadow._METRICS["eligible"] = 0
        shadow._METRICS["resolved"] = 0
        shadow._METRICS["restored"] = 0
        shadow._METRICS["outcomes"] = Counter()
        shadow._METRICS["scores"] = Counter()
        shadow._METRICS["nexus_approved"] = 0
        shadow._METRICS["nexus_vetoed"] = 0
        shadow._METRICS["nexus_timeout"] = 0
        shadow._METRICS["nexus_error"] = 0


def _install_strategy_fakes(score15=52, volume=0.91, entry_ok=True):
    originals = {
        "closed_mtf": shadow.closed_mtf,
        "detect_regime": strategy.detect_regime,
        "ema": strategy.ema,
        "score_tf": strategy.score_tf,
        "detect_entry": strategy.detect_entry,
        "atr": strategy.atr,
        "runtime": shadow._runtime_engine,
    }
    shadow.closed_mtf = lambda k15, k1h, k4h: (list(k15), list(k1h), list(k4h))
    strategy.detect_regime = lambda *args, **kwargs: "TRENDING_DOWN"
    # Bearish alignment requires EMA20 < EMA50 and price < EMA20.
    strategy.ema = lambda values, period: np.array([110.0 if period == 20 else 120.0])
    totals = iter((56, 71, score15))

    def fake_score(*args, **kwargs):
        total = next(totals)
        return {
            "ok": True,
            "total": total,
            "rsi_v": 47.0,
            "vol_r": volume,
            "aligned": True,
            "adx_v": 30.0,
            "summary": f"SCORE={total}",
        }

    strategy.score_tf = fake_score
    strategy.detect_entry = lambda *args, **kwargs: (entry_ok, "PULLBACK" if entry_ok else "NONE")
    strategy.atr = lambda *args, **kwargs: np.ones(80, dtype=float)
    shadow._runtime_engine = lambda: None
    return originals


def _restore_strategy(originals):
    shadow.closed_mtf = originals["closed_mtf"]
    strategy.detect_regime = originals["detect_regime"]
    strategy.ema = originals["ema"]
    strategy.score_tf = originals["score_tf"]
    strategy.detect_entry = originals["detect_entry"]
    strategy.atr = originals["atr"]
    shadow._runtime_engine = originals["runtime"]


def test_score_59_enrolls_only_when_every_other_strategy_gate_passes():
    _reset()
    originals = _install_strategy_fakes(score15=52, volume=0.91, entry_ok=True)
    try:
        shadow.observe(
            "DOGEUSDT", _bars(), _bars(), _bars(),
            production_result=None, min_score=60, fee_mult=2.0, log=_Log(),
        )
    finally:
        _restore_strategy(originals)
    snap = shadow.snapshot()
    assert snap["eligible"] == 1
    assert snap["active"] == 1
    state = next(iter(shadow._ACTIVE.values()))
    assert state["score"] == 59
    assert state["production_floor"] == 60
    assert state["floors_that_would_admit"] == [55, 56, 57, 58, 59]
    assert state["volume_ratio_15m"] == 0.91
    assert state["entry_type"] == "PULLBACK"
    assert state["rr"] == 2.0


def test_other_blocker_excludes_near_miss_from_score_floor_cohort():
    for volume, entry_ok in ((0.39, True), (0.91, False)):
        _reset()
        originals = _install_strategy_fakes(score15=52, volume=volume, entry_ok=entry_ok)
        try:
            shadow.observe(
                "DOGEUSDT", _bars(), _bars(), _bars(),
                production_result=None, min_score=60, fee_mult=2.0, log=_Log(),
            )
        finally:
            _restore_strategy(originals)
        assert shadow.snapshot()["eligible"] == 0
        assert shadow.snapshot()["active"] == 0


def test_outside_55_59_band_is_not_enrolled():
    # score15=41 -> round(14 + 21.3 + 18.45) = 54
    _reset()
    originals = _install_strategy_fakes(score15=41, volume=0.91, entry_ok=True)
    try:
        shadow.observe(
            "LINKUSDT", _bars(), _bars(), _bars(),
            production_result=None, min_score=60, fee_mult=2.0, log=_Log(),
        )
    finally:
        _restore_strategy(originals)
    assert shadow.snapshot()["eligible"] == 0


def test_nexus_counterfactual_uses_exact_validator_and_records_approval():
    _reset()
    key = "DOGEUSDT:SHORT:80"
    sig = strategy.Signal(
        symbol="DOGEUSDT", direction="SHORT", entry=100.0, sl=102.0, tp=96.0,
        confidence=0.59, score=59, expected_pnl=3.8, total_fees=0.12,
        entry_type="PULLBACK", regime="TRENDING_DOWN",
    )
    with shadow._LOCK:
        shadow._ACTIVE[key] = {
            "symbol": "DOGEUSDT", "direction": "SHORT", "score": 59,
            "production_floor": 60, "entry": 100.0, "sl": 102.0, "tp": 96.0,
            "nexus_status": "NOT_CHECKED", "nexus_approved": None,
        }

    class Engine:
        def __init__(self):
            self.calls = 0

        async def _nexus_validate(self, proposed):
            self.calls += 1
            return NexusDecision(
                symbol=proposed.symbol, decision=proposed.direction,
                confidence=70.0, setup_quality=68.0, data_quality=100.0,
                entry=proposed.entry, stop_loss=proposed.sl, take_profit=proposed.tp,
                expected_value=0.4, risk_reward=2.0, execution_allowed=True,
                reasoning=["approved test"],
            )

    engine = Engine()
    original_persist = shadow._schedule_persist
    shadow._schedule_persist = lambda *args, **kwargs: None
    try:
        asyncio.run(shadow._nexus_counterfactual(engine, sig, key, _Log()))
    finally:
        shadow._schedule_persist = original_persist
    assert engine.calls == 1
    state = shadow._ACTIVE[key]
    assert state["nexus_status"] == "APPROVED"
    assert state["nexus_approved"] is True
    assert shadow.snapshot()["nexus_approved"] == 1


def test_same_bar_ambiguity_is_stop_first():
    _reset()
    key = "DOGEUSDT:SHORT:1"
    with shadow._LOCK:
        shadow._ACTIVE[key] = {
            "symbol": "DOGEUSDT", "direction": "SHORT", "score": 59,
            "production_floor": 60, "entry": 100.0, "sl": 102.0, "tp": 96.0,
            "last_bar_ts": 1, "bars": 0, "mfe_pct": 0.0, "mae_pct": 0.0,
            "nexus_status": "VETOED", "nexus_approved": False,
        }
    original_closed = shadow.closed_mtf
    original_persist = shadow._schedule_persist
    shadow.closed_mtf = lambda *args: ([{"ts": 2, "h": 103.0, "l": 95.0, "c": 99.0}], [], [])
    shadow._schedule_persist = lambda *args, **kwargs: None
    try:
        shadow._update_outcomes("DOGEUSDT", [{}], [], [], _Log())
    finally:
        shadow.closed_mtf = original_closed
        shadow._schedule_persist = original_persist
    snap = shadow.snapshot()
    assert snap["outcomes"]["AMBIGUOUS_STOP_FIRST"] == 1
    assert snap["active"] == 0


def test_persistence_is_dedicated_and_restart_pending_nexus_fails_closed():
    original_exec = persistence.db._exec
    original_fetchall = persistence.db._fetchall
    original_ready = persistence._TABLE_READY
    calls = []

    async def fake_exec(sql, params=(), **kwargs):
        calls.append((sql, params))
        return True

    async def fake_fetchall(sql, params=(), **kwargs):
        payload = {
            "symbol": "DOGEUSDT", "direction": "SHORT", "score": 59,
            "production_floor": 60, "nexus_status": "IN_PROGRESS",
            "nexus_approved": None, "bars": 1,
        }
        import json
        return [("DOGEUSDT:SHORT:80", json.dumps(payload))]

    async def scenario():
        persistence._TABLE_READY = False
        persistence.db._exec = fake_exec
        persistence.db._fetchall = fake_fetchall
        try:
            assert await persistence.save_state(
                "DOGEUSDT:SHORT:80",
                {"symbol": "DOGEUSDT", "direction": "SHORT", "score": 59,
                 "production_floor": 60, "nexus_status": "NOT_CHECKED"},
                status="OPEN", log=_Log(),
            )
            return await persistence.load_open(_Log())
        finally:
            persistence.db._exec = original_exec
            persistence.db._fetchall = original_fetchall
            persistence._TABLE_READY = original_ready

    rows = asyncio.run(scenario())
    sql = "\n".join(call[0] for call in calls)
    assert "score_floor_shadow_audit" in sql
    assert "session_penalty_shadow_audit" not in sql
    assert rows[0][1]["nexus_status"] == "INTERRUPTED_RESTART"
    assert rows[0][1]["nexus_approved"] is False


def test_shadow_has_no_execution_or_threshold_mutation():
    source = inspect.getsource(shadow) + "\n" + inspect.getsource(persistence)
    forbidden = (
        "place_order", "create_order", "cancel_order", "close_position",
        "._open(", "cfg.LEVERAGE =", "MIN_ENTRY_SCORE =", "NEXUS_MIN_SCORE =",
        "MIN_VOLUME_MULT =", "_SESSION_PENALTY =", "TradingEngine.__init__ =",
    )
    assert all(token not in source for token in forbidden)
    assert "engine._nexus_validate(sig)" in source
    assert "decision_validation_error" in source
