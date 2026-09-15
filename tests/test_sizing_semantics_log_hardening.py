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
    assert "authoritative_policy=50pct_available_margin" in rec.msg
    assert "sizing_authority=OPERATOR_50PCT_EQUITY" in rec.msg
    assert "risk_manager_role=VALIDATION_GATE" in rec.msg
    assert "execution_effect=NONE" in rec.msg
    assert rec.args == ()


def test_runtime_contract_authority_is_corrected_in_place():
    rec = _record(
        "[RUNTIME_CONTRACT] status=PASS sizing_authority=RiskManagerV3_plus_operator_50pct_margin_cap"
    )
    normalize_record(rec)
    assert "sizing_authority=OPERATOR_50PCT_EQUITY" in rec.msg
    assert "risk_manager_role=VALIDATION_GATE" in rec.msg
    assert "RiskManagerV3_plus_operator_50pct_margin_cap" not in rec.msg


def test_legacy_pilot_install_wording_is_corrected():
    rec = _record("[PILOT_LIVE_RUNTIME] installed: 50pct-available position-notional sizing")
    normalize_record(rec)
    assert "50pct-available initial-margin sizing" in rec.msg
    assert "RiskManagerV3=VALIDATION_GATE" in rec.msg


def test_legacy_risk_cap_authority_is_corrected():
    rec = _record("[PILOT_RISK_CAP] installed: RiskManagerV3 is the maximum quantity authority")
    normalize_record(rec)
    assert "authoritative sizing=OPERATOR_50PCT_EQUITY" in rec.msg
    assert "validation-only" in rec.msg


def test_runtime_overlay_wording_is_corrected():
    rec = _record("[RUNTIME_OVERLAYS] final risk-authoritative sizing invariants active")
    normalize_record(rec)
    assert "operator-50pct-margin" in rec.msg
    assert "RiskManagerV3 validation gate" in rec.msg


def test_unrelated_log_is_unchanged():
    msg = "[KUCOIN] private websocket connected"
    rec = _record(msg)
    normalize_record(rec)
    assert rec.msg == msg
