from bot import nexus_terminal_notifications as terminal


class _DecisionWithBrokenReasoning:
    @property
    def reasoning(self):
        raise RuntimeError("telemetry-only getter failure")


class _SignalWithInvalidScore:
    score = "not-a-number"


def test_reason_fallback_survives_unreadable_reasoning_without_changing_decision():
    reason = terminal._reason_from_decision(_DecisionWithBrokenReasoning(), None)
    assert reason == "NEXUS não autorizou a execução"


def test_validation_reason_still_has_priority_over_reasoning():
    reason = terminal._reason_from_decision(
        _DecisionWithBrokenReasoning(), "explicit validation veto"
    )
    assert reason == "explicit validation veto"


def test_invalid_candidate_score_is_explicitly_unavailable():
    decision = type(
        "Decision",
        (),
        {
            "reasoning": [],
            "setup_quality": 0.0,
            "confidence": 0.0,
            "risk_reward": 0.0,
            "expected_value": 0.0,
        },
    )()
    metrics = terminal._reject_metrics(
        _SignalWithInvalidScore(), decision, "early veto"
    )
    assert metrics["candidate_score"] is None
    assert metrics["nexus_score"] is None
    assert metrics["confidence"] is None
    assert metrics["rr"] is None
    assert metrics["ev"] is None


def test_numeric_candidate_score_semantics_are_unchanged():
    sig = type("Signal", (), {"score": "73.5"})()
    decision = type(
        "Decision",
        (),
        {
            "reasoning": [],
            "setup_quality": 0.0,
            "confidence": 0.0,
            "risk_reward": 0.0,
            "expected_value": 0.0,
        },
    )()
    metrics = terminal._reject_metrics(sig, decision, "early veto")
    assert metrics["candidate_score"] == 73.5
