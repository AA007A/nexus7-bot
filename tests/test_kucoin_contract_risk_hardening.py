from bot.kucoin_contract_risk_hardening import (
    _contract_list,
    _normalize_mmr,
    _positive_finite,
    _select_cross_risk,
)


def test_normalize_mmr_accepts_fractional_exchange_value():
    assert _normalize_mmr(0.004) == 0.004
    assert _normalize_mmr("0.015") == 0.015


def test_normalize_mmr_rejects_missing_nonfinite_boolean_and_out_of_range():
    assert _normalize_mmr(None) is None
    assert _normalize_mmr(True) is None
    assert _normalize_mmr("nan") is None
    assert _normalize_mmr(0) is None
    assert _normalize_mmr(1) is None
    assert _normalize_mmr(-0.01) is None


def test_positive_finite_rejects_invalid_account_margin():
    assert _positive_finite("18.5482") == 18.5482
    assert _positive_finite(0) is None
    assert _positive_finite(-1) is None
    assert _positive_finite(False) is None
    assert _positive_finite("inf") is None


def test_contract_list_supports_kucoin_shapes_without_inventing_data():
    rows = [{"symbol": "ADAUSDTM", "maintainMargin": 0.015}]
    assert _contract_list(rows) is rows
    assert _contract_list({"data": rows}) is rows
    assert _contract_list({"dataList": rows}) is rows
    assert _contract_list({"items": rows}) is rows
    assert _contract_list({"data": {"symbol": "ADAUSDTM"}}) == []
    assert _contract_list(None) == []


def test_select_cross_risk_requires_exact_symbol_and_valid_mmr():
    rows = [
        {"symbol": "XBTUSDTM", "mmr": "0.0041", "leverage": "50"},
        {
            "symbol": "ADAUSDTM",
            "mmr": "0.0123",
            "imr": "0.0200",
            "totalMargin": "18.5482",
            "price": "0.205",
            "leverage": "50.00",
        },
    ]
    risk = _select_cross_risk(rows, "ADAUSDTM")
    assert risk["mmr"] == 0.0123
    assert risk["imr"] == 0.02
    assert risk["totalMargin"] == 18.5482
    assert risk["leverage"] == 50.0
    assert _select_cross_risk(rows, "ETHUSDTM") is None


def test_select_cross_risk_fails_closed_on_invalid_exact_row():
    rows = [
        {"symbol": "ADAUSDTM", "mmr": "nan", "leverage": "50"},
        {"symbol": "XBTUSDTM", "mmr": "0.004", "leverage": "50"},
    ]
    assert _select_cross_risk({"data": rows}, "ADAUSDTM") is None
