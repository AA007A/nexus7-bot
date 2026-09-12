import inspect

from bot import indicators
from bot import missed_opportunity_audit as audit
from bot import nexus_decision_consistency as consistency


class _Client:
    def get_cached_klines(self, symbol, interval, limit):
        return [{"ts": 1_700_000_000_000 + i * 900_000,
                 "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0}
                for i in range(30)]


class _Engine:
    client = _Client()


class _Sig:
    symbol = "LINKUSDT"
    direction = "SHORT"


class _Decision:
    models = [
        {"name": "TREND", "direction": "SHORT", "confidence": 45.0,
         "risk": 9.9, "available": True, "reason": "EMAs bearish"},
        {"name": "STRUCTURE", "direction": "LONG", "confidence": 70.0,
         "risk": 25.0, "available": True, "reason": "UPTREND"},
    ]


def _patch_smc(payload):
    old_closed = consistency.closed_mtf
    old_smc = indicators.smc_analysis
    consistency.closed_mtf = lambda k15, k1h, k4h: (list(k15), list(k1h), list(k4h))
    indicators.smc_analysis = lambda highs, lows, closes: dict(payload)
    return old_closed, old_smc


def _restore(old_closed, old_smc):
    consistency.closed_mtf = old_closed
    indicators.smc_analysis = old_smc


def test_structure_conflict_no_bos_is_classified_as_local_pullback_risk():
    old_closed, old_smc = _patch_smc({
        "structure": "UPTREND", "hh": True, "hl": True,
        "lh": False, "ll": False, "bos": False, "bos_dir": "NONE",
        "choch": False, "last_swing_high": 101.0, "last_swing_low": 99.0,
    })
    try:
        ctx = audit._structure_conflict_context(
            _Engine(), _Sig(), _Decision(),
            "Ensemble (LONG) diverge do MTF (SHORT)",
        )
    finally:
        _restore(old_closed, old_smc)

    assert ctx["classification"] == "LOCAL_STRUCTURE_OPPOSES_MTF_NO_BOS"
    assert ctx["candidate_mtf_direction"] == "SHORT"
    assert ctx["structure_model_direction"] == "LONG"
    assert ctx["smc_15m"]["hh"] is True
    assert ctx["smc_15m"]["hl"] is True
    assert ctx["smc_15m"]["bos"] is False
    assert ctx["trend_model"]["direction"] == "SHORT"


def test_structure_conflict_with_bos_is_kept_separate_from_no_bos_cohort():
    old_closed, old_smc = _patch_smc({
        "structure": "UPTREND", "hh": True, "hl": True,
        "lh": False, "ll": False, "bos": True, "bos_dir": "BULLISH",
        "choch": False, "last_swing_high": 101.0, "last_swing_low": 99.0,
    })
    try:
        ctx = audit._structure_conflict_context(
            _Engine(), _Sig(), _Decision(),
            "Ensemble (LONG) diverge do MTF (SHORT)",
        )
    finally:
        _restore(old_closed, old_smc)

    assert ctx["classification"] == "LOCAL_STRUCTURE_OPPOSES_MTF_WITH_BOS"
    assert ctx["smc_15m"]["bos_dir"] == "BULLISH"


def test_structure_conflict_choch_has_its_own_cohort():
    old_closed, old_smc = _patch_smc({
        "structure": "UPTREND", "hh": True, "hl": True,
        "lh": False, "ll": False, "bos": True, "bos_dir": "BEARISH",
        "choch": True, "last_swing_high": 101.0, "last_swing_low": 99.0,
    })
    try:
        ctx = audit._structure_conflict_context(
            _Engine(), _Sig(), _Decision(),
            "Ensemble (LONG) diverge do MTF (SHORT)",
        )
    finally:
        _restore(old_closed, old_smc)

    assert ctx["classification"] == "LOCAL_STRUCTURE_OPPOSES_MTF_CHOCH"
    assert ctx["smc_15m"]["choch"] is True


def test_non_mtf_veto_produces_no_structure_conflict_shadow():
    ctx = audit._structure_conflict_context(
        _Engine(), _Sig(), _Decision(),
        "R:R líquido 1.49 < mínimo líquido 1.60",
    )
    assert ctx == {}


def test_structure_conflict_shadow_cannot_mutate_execution_or_thresholds():
    source = inspect.getsource(audit)
    forbidden = (
        "place_order", "create_order", "cancel_order", "close_position",
        "cfg.LEVERAGE =", "NEXUS_MIN_SCORE =", "NEXUS_MIN_RR_NET =",
        "MIN_ENTRY_SCORE =", "MIN_VOLUME_MULT =",
    )
    assert all(token not in source for token in forbidden)
