from types import SimpleNamespace

from bot import nexus_optional_evidence as optional


WEIGHTS = {
    "TREND_ALIGNMENT": 0.15,
    "MOMENTUM": 0.10,
    "VOLUME": 0.10,
    "MARKET_STRUCTURE": 0.15,
    "VOLATILITY": 0.10,
    "DERIVATIVES": 0.10,
    "MICROSTRUCTURE": 0.10,
    "MULTI_TIMEFRAME": 0.10,
    "RISK_REWARD": 0.10,
}


def _log():
    return SimpleNamespace(warning=lambda *args, **kwargs: None)


def _model(direction="WAIT", confidence=0.0, available=True):
    return SimpleNamespace(
        name="DERIVATIVES",
        direction=SimpleNamespace(value=direction),
        confidence=confidence,
        available=available,
    )


def _components():
    return {
        "TREND_ALIGNMENT": 80.0,
        "MOMENTUM": 70.0,
        "VOLUME": 60.0,
        "MARKET_STRUCTURE": 75.0,
        "VOLATILITY": 90.0,
        "DERIVATIVES": 0.0,
        "MICROSTRUCTURE": 0.0,
        "MULTI_TIMEFRAME": 100.0,
        "RISK_REWARD": 85.0,
    }


def _base_score_result():
    # Legacy behavior already excludes MICROSTRUCTURE but keeps an available
    # neutral DERIVATIVES component at zero.
    comps = _components()
    active = [k for k in comps if k != "MICROSTRUCTURE"]
    weight = sum(WEIGHTS[k] for k in active)
    total = sum(comps[k] * WEIGHTS[k] / weight for k in active)
    return {
        "components": comps,
        "unavailable": ["MICROSTRUCTURE"],
        "weight_used": round(weight, 3),
        "total": round(total, 2),
    }


def test_optional_missing_is_reported_without_global_dq_penalty():
    def legacy_validate(*args, **kwargs):
        # Core data is perfect; legacy optional penalties: funding 3 + OI 3.
        return SimpleNamespace(
            score=94.0,
            unavailable=["funding", "open_interest"],
            stale=[], errors=[], is_acceptable=True,
        )

    fake_ai = SimpleNamespace(
        validate_data=legacy_validate,
        _score_components=lambda *args, **kwargs: _base_score_result(),
        WEIGHTS=WEIGHTS,
    )
    optional.install(fake_ai, _log())
    dq = fake_ai.validate_data(
        "NEARUSDT", [], [], [],
        ticker={"lastPrice": "2.7"}, funding=None, oi=None,
        orderbook={"b": [["2.7", "1"]]},
    )
    assert dq.score == 100.0
    assert dq.unavailable == ["funding", "open_interest"]
    assert dq.optional_unavailable_penalty_removed == 6.0


def test_core_data_penalty_is_never_restored():
    def legacy_validate(*args, **kwargs):
        # 20 points are from core candle integrity + 6 optional points.
        return SimpleNamespace(
            score=74.0,
            unavailable=["funding", "open_interest"],
            stale=["15m(999s)"], errors=[], is_acceptable=True,
        )

    fake_ai = SimpleNamespace(
        validate_data=legacy_validate,
        _score_components=lambda *args, **kwargs: _base_score_result(),
        WEIGHTS=WEIGHTS,
    )
    optional.install(fake_ai, _log())
    dq = fake_ai.validate_data(
        "NEARUSDT", [], [], [],
        ticker={"lastPrice": "2.7"}, funding=None, oi=None,
        orderbook={"b": [["2.7", "1"]]},
    )
    assert dq.score == 80.0
    assert dq.score < 100.0


def test_all_missing_optional_fields_restore_only_capped_legacy_12_points():
    def legacy_validate(*args, **kwargs):
        return SimpleNamespace(
            score=88.0,
            unavailable=["ticker", "funding", "open_interest", "orderbook"],
            stale=[], errors=[], is_acceptable=True,
        )

    fake_ai = SimpleNamespace(
        validate_data=legacy_validate,
        _score_components=lambda *args, **kwargs: _base_score_result(),
        WEIGHTS=WEIGHTS,
    )
    optional.install(fake_ai, _log())
    dq = fake_ai.validate_data("ETHUSDT", [], [], [])
    assert dq.score == 100.0
    assert dq.optional_unavailable_penalty_removed == 12.0


def test_neutral_derivatives_are_excluded_and_weights_renormalized():
    base = _base_score_result()
    result = optional._renormalize_without_neutral_derivatives(
        base, WEIGHTS, [_model(direction="WAIT", confidence=0.0, available=True)]
    )

    assert result["neutral"] == ["DERIVATIVES"]
    assert result["unavailable"] == ["MICROSTRUCTURE"]
    assert result["weight_used"] == 0.8
    expected = sum(
        value * WEIGHTS[key]
        for key, value in _components().items()
        if key not in {"DERIVATIVES", "MICROSTRUCTURE"}
    ) / 0.8
    assert result["total"] == round(expected, 2)
    assert result["total"] > base["total"]


def test_opposing_derivatives_remain_active_and_keep_penalty():
    base = _base_score_result()
    result = optional._renormalize_without_neutral_derivatives(
        base, WEIGHTS, [_model(direction="SHORT", confidence=55.0, available=True)]
    )
    assert result == base


def test_aligned_derivatives_remain_active():
    base = _base_score_result()
    result = optional._renormalize_without_neutral_derivatives(
        base, WEIGHTS, [_model(direction="LONG", confidence=55.0, available=True)]
    )
    assert result == base


def test_unavailable_derivatives_are_left_to_existing_exclusion_logic():
    base = _base_score_result()
    base["unavailable"] = ["DERIVATIVES", "MICROSTRUCTURE"]
    result = optional._renormalize_without_neutral_derivatives(
        base, WEIGHTS, [_model(available=False)]
    )
    assert result == base


def test_install_is_idempotent():
    calls = {"validate": 0, "score": 0}

    def legacy_validate(*args, **kwargs):
        calls["validate"] += 1
        return SimpleNamespace(score=100.0, unavailable=[], stale=[], errors=[])

    def legacy_score(*args, **kwargs):
        calls["score"] += 1
        return _base_score_result()

    fake_ai = SimpleNamespace(
        validate_data=legacy_validate,
        _score_components=legacy_score,
        WEIGHTS=WEIGHTS,
    )
    optional.install(fake_ai, _log())
    first_validate = fake_ai.validate_data
    first_score = fake_ai._score_components
    optional.install(fake_ai, _log())
    assert fake_ai.validate_data is first_validate
    assert fake_ai._score_components is first_score

    fake_ai.validate_data(
        "BTCUSDT", [], [], [], ticker={"lastPrice": "1"}, funding=0.0,
        oi={}, orderbook={"b": [["1", "1"]]},
    )
    fake_ai._score_components(
        [_model(direction="WAIT")], {}, 2.0, SimpleNamespace(value="LONG"), None
    )
    assert calls == {"validate": 1, "score": 1}
