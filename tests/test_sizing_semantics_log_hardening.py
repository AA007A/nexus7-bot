import logging

from bot.sizing_semantics_log_hardening import SizingSemanticsFilter, normalize_record


def _record(msg, args=()):
    return logging.LogRecord("test.child", logging.WARNING, __file__, 1, msg, args, None)


def test_legacy_target_is_relabelled_without_execution_claims():
    rec = _record(
        "[PILOT_LEGACY_TARGET] preliminary_only=true target_notional=%.4f leverage=%sx",
        (9.68, 50),
    )
    assert SizingSemanticsFilter().filter(rec) is True
    assert "sizing_authority=RISK_POLICY_MIN_OF_CAPS" in rec.msg
    assert "operator_margin=CAP_ONLY" in rec.msg
    assert "execution_effect=NONE" in rec.msg
    assert "OPERATOR_50PCT_EQUITY" not in rec.msg
    assert rec.args == ()


def test_current_authority_messages_are_never_rewritten():
    for msg in (
        "[RUNTIME_CONTRACT] status=PASS sizing_authority=RISK_POLICY_MIN_OF_CAPS operator_margin=CAP_ONLY",
        "[RUNTIME_OVERLAYS] final risk-authoritative sizing invariants active",
        "[FINAL_SIZING_INVARIANT] installed=true sizing_authority=RISK_POLICY_MIN_OF_CAPS",
    ):
        rec = _record(msg)
        normalize_record(rec)
        assert rec.msg == msg
        assert "OPERATOR_50PCT_EQUITY" not in rec.msg


def test_legacy_pilot_install_wording_is_corrected():
    rec = _record("[PILOT_LIVE_RUNTIME] installed: 50pct-available position-notional sizing")
    normalize_record(rec)
    assert "CAP only" in rec.msg
    assert "RISK_POLICY_MIN_OF_CAPS" in rec.msg


def test_legacy_risk_cap_wording_points_to_final_authority():
    rec = _record("[PILOT_RISK_CAP] installed: RiskManagerV3 is the maximum quantity authority")
    normalize_record(rec)
    assert "FINAL_SIZING_INVARIANT" in rec.msg
    assert "RISK_POLICY_MIN_OF_CAPS" in rec.msg


def test_unrelated_log_is_unchanged():
    msg = "[KUCOIN] private websocket connected"
    rec = _record(msg)
    normalize_record(rec)
    assert rec.msg == msg
