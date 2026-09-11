from bot.kucoin_contract_risk_hardening import _contract_list, _normalize_mmr


def test_normalize_mmr_accepts_fractional_public_contract_value():
    assert _normalize_mmr(0.004) == 0.004
    assert _normalize_mmr("0.015") == 0.015


def test_normalize_mmr_rejects_missing_nonfinite_boolean_and_out_of_range():
    assert _normalize_mmr(None) is None
    assert _normalize_mmr(True) is None
    assert _normalize_mmr("nan") is None
    assert _normalize_mmr(0) is None
    assert _normalize_mmr(1) is None
    assert _normalize_mmr(-0.01) is None


def test_contract_list_supports_kucoin_shapes_without_inventing_data():
    rows = [{"symbol": "ADAUSDTM", "maintainMargin": 0.015}]
    assert _contract_list(rows) is rows
    assert _contract_list({"data": rows}) is rows
    assert _contract_list({"dataList": rows}) is rows
    assert _contract_list({"items": rows}) is rows
    assert _contract_list({"data": {"symbol": "ADAUSDTM"}}) == []
    assert _contract_list(None) == []
