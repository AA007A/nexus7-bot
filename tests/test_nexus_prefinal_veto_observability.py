from types import SimpleNamespace

from bot import nexus_prefinal_veto_observability as obs


class _Log:
    def __init__(self):
        self.rows = []

    def info(self, *args):
        self.rows.append(args)


def _decision(*, allowed=False, score=0.0, models=None, reasoning=None):
    return SimpleNamespace(
        symbol="NEARUSDT",
        decision="WAIT" if not allowed else "LONG",
        execution_allowed=allowed,
        setup_quality=score,
        models=models or [],
        reasoning=reasoning or [],
    )


def test_compact_models_preserves_direction_confidence_risk_and_reason():
    rows = obs._compact_models([
        {
            "name": "MOMENTUM",
            "direction": "SHORT",
            "confidence": 60,
            "risk": 25,
            "available": True,
            "reason": "RSI/MACD bearish",
        }
    ])
    assert rows == [{
        "name": "MOMENTUM",
        "direction": "SHORT",
        "confidence": 60.0,
        "risk": 25.0,
        "available": True,
        "reason": "RSI/MACD bearish",
    }]


def test_early_veto_logs_attached_model_snapshot_without_changing_decision():
    d = _decision(
        score=0.0,
        reasoning=["Regime: TRENDING_BULL", "Ensemble (SHORT) diverge do MTF (LONG)"],
        models=[{
            "name": "MOMENTUM",
            "direction": "SHORT",
            "confidence": 60,
            "risk": 25,
            "available": True,
            "reason": "bearish",
        }],
    )
    fake_ai = SimpleNamespace(decide=lambda *a, **k: d)
    log = _Log()
    obs.install(fake_ai, log)

    result = fake_ai.decide(symbol="NEARUSDT")
    assert result is d
    assert any("[NEXUS_PREFINAL_VETO_MODELS]" in str(row[0]) for row in log.rows)
    payload = next(row for row in log.rows if "[NEXUS_PREFINAL_VETO_MODELS]" in str(row[0]))
    assert "Ensemble (SHORT) diverge do MTF (LONG)" in payload


def test_final_score_rejection_does_not_duplicate_prefinal_log():
    d = _decision(
        score=54.7,
        reasoning=["REJEITADO: score 54.7 < 60"],
        models=[{"name": "TREND", "direction": "LONG", "confidence": 70}],
    )
    fake_ai = SimpleNamespace(decide=lambda *a, **k: d)
    log = _Log()
    obs.install(fake_ai, log)
    fake_ai.decide(symbol="NEARUSDT")

    assert not any("[NEXUS_PREFINAL_VETO_MODELS]" in str(row[0]) for row in log.rows)


def test_allowed_decision_never_logs_prefinal_veto():
    d = _decision(
        allowed=True,
        score=67.0,
        models=[{"name": "TREND", "direction": "LONG", "confidence": 80}],
    )
    fake_ai = SimpleNamespace(decide=lambda *a, **k: d)
    log = _Log()
    obs.install(fake_ai, log)
    fake_ai.decide(symbol="NEARUSDT")

    assert not any("[NEXUS_PREFINAL_VETO_MODELS]" in str(row[0]) for row in log.rows)


def test_install_is_idempotent():
    calls = {"n": 0}
    d = _decision()

    def decide(*args, **kwargs):
        calls["n"] += 1
        return d

    fake_ai = SimpleNamespace(decide=decide)
    log = _Log()
    obs.install(fake_ai, log)
    first = fake_ai.decide
    obs.install(fake_ai, log)
    assert fake_ai.decide is first
    fake_ai.decide(symbol="NEARUSDT")
    assert calls["n"] == 1
