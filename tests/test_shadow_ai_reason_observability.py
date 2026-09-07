from types import SimpleNamespace

from bot.shadow_live import _shadow_ai_reason


def test_approved_reason_is_approved():
    decision = SimpleNamespace(reasoning=["unused"], warnings=[])
    assert _shadow_ai_reason(decision, None, True) == "approved"


def test_legitimate_veto_reports_nexus_reasoning():
    decision = SimpleNamespace(
        reasoning=["Ensemble (SHORT) diverge do MTF (LONG)"],
        warnings=[],
    )
    reason = _shadow_ai_reason(decision, None, False)
    assert reason == "Ensemble (SHORT) diverge do MTF (LONG)"
    assert reason != "approved"


def test_validation_failure_has_priority():
    decision = SimpleNamespace(reasoning=["strategy reason"], warnings=[])
    assert _shadow_ai_reason(decision, "symbol_mismatch", False) == "symbol_mismatch"


def test_veto_without_reason_is_explicit():
    decision = SimpleNamespace(reasoning=[], warnings=[])
    assert _shadow_ai_reason(decision, None, False) == "nexus_ai_veto"


def test_reason_is_bounded_for_logs():
    decision = SimpleNamespace(reasoning=["x" * 500], warnings=[])
    assert len(_shadow_ai_reason(decision, None, False)) == 240
