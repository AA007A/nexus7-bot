"""Regression tests for the narrow NEXUS HTF/regime transition guard.

These tests intentionally avoid live/exchange dependencies. The guard may only
lift the legacy opposite-regime compatibility from 25 to 35 when confirmed
4H/1H/15m EMA structure is unanimously aligned and opposite ADX strength is in
the narrow transition band 25 < ADX <= 35. It must never change thresholds,
leverage or routing.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

from bot import nexus_regime_transition_consistency as guard
from bot import runtime_overlays


class _Value:
    def __init__(self, value: str):
        self.value = value

    def __eq__(self, other):
        return isinstance(other, _Value) and self.value == other.value


BULL = _Value("TRENDING_BULL")
BEAR = _Value("TRENDING_BEAR")
LONG = _Value("LONG")
SHORT = _Value("SHORT")


class _Log:
    def __init__(self):
        self.messages = []

    def warning(self, message, *args):
        self.messages.append(message % args if args else message)


def _mtf(direction, *, score=100.0, conflict=False, available=True, detail=None):
    value = direction.value
    return {
        "direction": direction,
        "score": score,
        "conflict": conflict,
        "available": available,
        "detail": detail or f"4H={value} 1H={value} 15M={value}",
    }


def _fake_nexus(*, regime=BULL, direction=SHORT, adx=30.0, mtf=None, base=25.0):
    mtf_value = mtf or _mtf(direction)
    return SimpleNamespace(
        analyze_mtf=lambda *a, **k: mtf_value,
        detect_regime=lambda *a, **k: (regime, {"adx": adx}),
        regime_compatibility=lambda r, d: base,
    )


def test_transition_short_inside_bull_is_allowed_only_in_narrow_band():
    assert guard._transition_allowed(BULL, SHORT, _mtf(SHORT), {"adx": 30.0}) is True
    assert guard._transition_allowed(BULL, SHORT, _mtf(SHORT), {"adx": 25.0}) is False
    assert guard._transition_allowed(BULL, SHORT, _mtf(SHORT), {"adx": 35.01}) is False


def test_transition_long_inside_bear_is_symmetric():
    assert guard._transition_allowed(BEAR, LONG, _mtf(LONG), {"adx": 31.0}) is True
    assert guard._transition_allowed(BEAR, SHORT, _mtf(SHORT), {"adx": 31.0}) is False


def test_transition_requires_exact_three_timeframe_alignment():
    assert guard._transition_allowed(BULL, SHORT, _mtf(SHORT, score=70.0), {"adx": 30.0}) is False
    assert guard._transition_allowed(BULL, SHORT, _mtf(SHORT, conflict=True), {"adx": 30.0}) is False
    assert guard._transition_allowed(BULL, SHORT, _mtf(SHORT, available=False), {"adx": 30.0}) is False
    partial = _mtf(SHORT, detail="4H=SHORT 1H=SHORT 15M=WAIT")
    assert guard._transition_allowed(BULL, SHORT, partial, {"adx": 30.0}) is False


def test_install_lifts_only_the_specific_hard_veto_to_35():
    nexus = _fake_nexus(adx=30.0)
    log = _Log()
    guard.install(nexus, log)

    mtf = nexus.analyze_mtf([], [], [])
    assert mtf["score"] == 100.0
    regime, details = nexus.detect_regime([], [], [], [])
    assert regime == BULL and details["adx"] == 30.0
    assert nexus.regime_compatibility(BULL, SHORT) == 35.0
    assert any("NEXUS_REGIME_TRANSITION" in message for message in log.messages)


def test_install_preserves_hard_veto_when_opposite_adx_is_established():
    nexus = _fake_nexus(adx=40.0)
    guard.install(nexus, _Log())
    nexus.analyze_mtf([], [], [])
    nexus.detect_regime([], [], [], [])
    assert nexus.regime_compatibility(BULL, SHORT) == 25.0


def test_install_preserves_non_hard_base_compatibility():
    nexus = _fake_nexus(adx=30.0, base=60.0)
    guard.install(nexus, _Log())
    nexus.analyze_mtf([], [], [])
    nexus.detect_regime([], [], [], [])
    assert nexus.regime_compatibility(BULL, SHORT) == 60.0


def test_runtime_overlay_installs_guard_last_without_threshold_or_leverage_mutation():
    overlay_source = inspect.getsource(runtime_overlays.install)
    guard_source = inspect.getsource(guard)

    assert "regime_transition.install(nexus_ai, log)" in overlay_source
    assert "NEXUS_MIN_RR_NET" not in guard_source
    assert "NEXUS_MIN_SCORE" not in guard_source
    assert "LEVERAGE =" not in guard_source
    assert "cfg.LEVERAGE" not in guard_source
    assert "place_order" not in guard_source
    assert "create_order" not in guard_source
