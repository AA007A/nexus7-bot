from types import SimpleNamespace

from bot import market_risk_coverage_observability as coverage


def test_degraded_provider_is_observable_without_execution_authority():
    assessment = SimpleNamespace(
        score=0,
        level="NORMAL",
        block_new_entries=False,
        size_multiplier=1.0,
    )
    snap = {
        "fresh": True,
        "signals": {
            "funding_rate_pct": 0.01,
            "us10y_yield_change_bps": 2.0,
        },
        "stale_reasons": {},
        "providers": {
            "coinglass_v4": "no_valid_signals:http=[200, 200]:api=['401', '401']:fail_neutral",
            "yahoo_finance_daily": "stale_source:4/4:fail_neutral",
            "us_treasury_curve": "ok_source_fresh",
        },
        "assessment": assessment,
    }

    result = coverage.classify_coverage(snap)

    assert result["coverage_state"] == "DEGRADED"
    assert result["coverage_execution_effect"] == "NONE"
    assert set(result["degraded_providers"]) == {
        "coinglass_v4",
        "yahoo_finance_daily",
    }
    assert assessment.block_new_entries is False
    assert assessment.size_multiplier == 1.0


def test_optional_disabled_coinglass_is_not_treated_as_outage():
    result = coverage.classify_coverage({
        "signals": {"us10y_yield_change_bps": 1.0},
        "stale_reasons": {},
        "providers": {
            "coinglass_v4": "disabled_no_key",
            "us_treasury_curve": "ok_source_fresh",
        },
    })
    assert result["coverage_state"] == "NORMAL"
    assert result["degraded_providers"] == ()


def test_stale_signal_marks_coverage_degraded_but_does_not_modify_assessment():
    assessment = SimpleNamespace(
        score=12,
        level="NORMAL",
        block_new_entries=False,
        size_multiplier=1.0,
    )
    snap = {
        "signals": {"funding_rate_pct": 0.01},
        "stale_reasons": {"spx_change_pct": "SOURCE_AGE"},
        "providers": {"us_treasury_curve": "ok_source_fresh"},
        "assessment": assessment,
    }

    result = coverage.classify_coverage(snap)
    assert result["coverage_state"] == "DEGRADED"
    assert "STALE_SIGNALS" in result["coverage_reasons"]
    assert assessment.score == 12
    assert assessment.block_new_entries is False
    assert assessment.size_multiplier == 1.0


def test_install_annotates_snapshot_logs_state_and_preserves_assessment_identity():
    assessment = SimpleNamespace(
        score=0,
        level="NORMAL",
        block_new_entries=False,
        size_multiplier=1.0,
    )

    class DummyRuntime:
        _coverage_observability_installed = False

        @staticmethod
        def snapshot(*args, **kwargs):
            return {
                "signals": {"funding_rate_pct": 0.01},
                "stale_reasons": {},
                "providers": {"yahoo_finance_daily": "unavailable_fail_neutral"},
                "assessment": assessment,
            }

    class DummyLog:
        def __init__(self):
            self.info_calls = []
            self.warning_calls = []

        def info(self, *args, **kwargs):
            self.info_calls.append(args)

        def warning(self, *args, **kwargs):
            self.warning_calls.append(args)

    runtime = DummyRuntime()
    log = DummyLog()
    coverage.install(runtime, log)
    snap = runtime.snapshot()

    assert snap["assessment"] is assessment
    assert snap["coverage_state"] == "DEGRADED"
    assert snap["coverage_execution_effect"] == "NONE"
    assert snap["assessment"].block_new_entries is False
    assert snap["assessment"].size_multiplier == 1.0
    assert len(log.warning_calls) == 1
    assert "[MARKET_RISK_COVERAGE_STATE]" in log.warning_calls[0][0]

    # Same state is rate-limited rather than spamming every PilotGuard evaluate().
    runtime.snapshot()
    assert len(log.warning_calls) == 1
