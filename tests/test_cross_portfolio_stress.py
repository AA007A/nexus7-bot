import asyncio
from types import SimpleNamespace

from bot import cross_portfolio_stress as stress
from bot.config import cfg


class _Client:
    def __init__(self, *, margin="25", active_orders=None, high_mmr=False):
        self.margin = margin
        self.active_orders = active_orders or []
        self.high_mmr = high_mmr
        self.post_calls = []

    async def _get(self, path, params=None, auth=False):
        if path == "/api/v1/account-overview":
            return {"marginBalance": self.margin}
        if path == "/api/v1/orders":
            return {"items": self.active_orders}
        if path == "/api/v1/positions":
            return [{
                "symbol": "NEARUSDTM",
                "currentQty": "100",
                "crossMode": True,
                "marginMode": "CROSS",
                "markPrice": "2.50",
                "markValue": "25.00",
                "maintMarginReq": "0.010",
            }]
        raise AssertionError(path)

    async def _post(self, path, body, **kwargs):
        assert path == "/api/v2/getCrossModeMarginRequirement"
        self.post_calls.append(dict(body))
        mmr = "0.50" if self.high_mmr else "0.010"
        return [{
            "symbol": body["symbol"],
            "positionValue": body["positionValue"],
            "mmr": mmr,
            "imr": "0.020",
        }]


class _Position:
    symbol = "NEARUSDT"
    direction = "LONG"
    sl = 2.40
    trailing_sl = 2.40


class _Signal:
    symbol = "ETHUSDT"
    direction = "LONG"
    entry = 3000.0
    sl = 2940.0


def _engine(client):
    return SimpleNamespace(
        client=client,
        positions={"NEARUSDT": _Position()},
        instruments={
            "NEARUSDT": {"kucoinSymbol": "NEARUSDTM", "multiplier": 0.1},
            "ETHUSDT": {"kucoinSymbol": "ETHUSDTM", "multiplier": 0.001},
        },
    )


def test_stop_stress_passes_only_with_exact_private_cross_state_and_headroom():
    client = _Client()
    result = asyncio.run(stress.evaluate(_engine(client), _Signal(), 0.002))

    assert result.allowed is True
    assert result.reason == "stop_stress_headroom_ok"
    assert 0 <= result.risk_rate < stress.MAX_STOP_STRESS_RISK_RATE
    assert result.existing_positions == 1
    assert len(client.post_calls) == 2
    assert all(call["leverage"] == str(int(cfg.LEVERAGE)) for call in client.post_calls)


def test_nonreduce_active_order_fails_closed_before_margin_projection():
    client = _Client(active_orders=[{
        "id": "other-order",
        "isActive": True,
        "status": "open",
        "reduceOnly": False,
    }])
    result = asyncio.run(stress.evaluate(_engine(client), _Signal(), 0.002))

    assert result.allowed is False
    assert result.reason == "additional_active_order_exposure"
    assert client.post_calls == []


def test_reduce_only_active_order_does_not_add_new_exposure():
    client = _Client(active_orders=[{
        "id": "protective-close",
        "isActive": True,
        "status": "open",
        "reduceOnly": True,
    }])
    result = asyncio.run(stress.evaluate(_engine(client), _Signal(), 0.002))
    assert result.allowed is True


def test_high_stressed_risk_rate_blocks_second_position():
    client = _Client(margin="1.20", high_mmr=True)
    result = asyncio.run(stress.evaluate(_engine(client), _Signal(), 0.002))

    assert result.allowed is False
    assert result.reason in {"stop_stress_risk_rate_too_high", "nonpositive_stressed_margin"}


def test_missing_local_protective_stop_fails_closed():
    client = _Client()
    position = _Position()
    position.sl = 0
    position.trailing_sl = 0
    engine = _engine(client)
    engine.positions = {"NEARUSDT": position}

    result = asyncio.run(stress.evaluate(engine, _Signal(), 0.002))
    assert result.allowed is False
    assert result.reason == "existing_stop_not_adverse_or_missing"


def test_exchange_local_position_mismatch_fails_closed():
    client = _Client()
    engine = _engine(client)
    engine.positions = {
        "NEARUSDT": _Position(),
        "BTCUSDT": SimpleNamespace(direction="LONG", sl=60000, trailing_sl=60000),
    }
    result = asyncio.run(stress.evaluate(engine, _Signal(), 0.002))
    assert result.allowed is False
    assert result.reason == "expected_exactly_one_existing_position"


def test_install_marks_capability_but_preserves_pre_sizing_refresh():
    from bot import pilot_risk_cap_hardening as risk_cap

    events = []

    class Engine:
        _cross_portfolio_stress_installed = False

        async def _refresh_entry_balance(self):
            events.append("refresh")
            return True

    class Log:
        def warning(self, *args, **kwargs):
            pass
        def critical(self, *args, **kwargs):
            pass

    stress.install(Engine, Log())
    instance = Engine()

    tok_sig = risk_cap._PILOT_SIGNAL.set(None)
    tok_qty = risk_cap._PILOT_FINAL_QTY.set(None)
    try:
        assert asyncio.run(instance._refresh_entry_balance()) is True
    finally:
        risk_cap._PILOT_FINAL_QTY.reset(tok_qty)
        risk_cap._PILOT_SIGNAL.reset(tok_sig)

    assert events == ["refresh"]
    assert Engine._cross_portfolio_stress_capable is True
    assert Engine._cross_portfolio_stress_installed is True
