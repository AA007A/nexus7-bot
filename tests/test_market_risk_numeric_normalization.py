from bot import market_risk_runtime as runtime


def _payload(funding):
    return {
        "code": "0",
        "data": [
            {
                "symbol": "BTC",
                "avg_funding_rate_by_oi": funding,
                "open_interest_usd": 0,
            }
        ],
    }


def test_finite_number_accepts_valid_numeric_evidence():
    assert runtime._finite_number("0.0125") == 0.0125
    assert runtime._finite_number(0) == 0.0


def test_finite_number_rejects_invalid_nonfinite_evidence():
    assert runtime._finite_number(None) is None
    assert runtime._finite_number("not-a-number") is None
    assert runtime._finite_number(float("nan")) is None
    assert runtime._finite_number(float("inf")) is None
    assert runtime._finite_number(float("-inf")) is None


def test_invalid_coinglass_funding_is_fail_neutral_without_fake_zero():
    for value in (None, "bad", float("nan"), float("inf")):
        parsed = runtime.parse_coinglass_markets(_payload(value), now=1000)
        assert "funding_rate_pct" not in parsed


def test_valid_coinglass_funding_semantics_are_unchanged():
    parsed = runtime.parse_coinglass_markets(_payload("0.08"), now=1000)
    assert parsed["funding_rate_pct"] == 0.08
