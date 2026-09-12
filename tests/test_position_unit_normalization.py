import asyncio
import inspect

from bot import nexus_runtime_engine
from bot.kucoin_position_units import KuCoinPositionUnitAdapter
from bot.quantity import base_to_contracts, contracts_to_base


INFO = {
    "multiplier": "0.1",
    "lotSize": "1",
    "qtyStep": "1",
    "minQty": "1",
    "minNotional": "0",
}


class FakeKuCoinClient:
    def __init__(self, rows=None, instruments=None):
        self.rows = rows if rows is not None else []
        self._instruments = instruments if instruments is not None else {"LTCUSDT": dict(INFO)}
        self.marker = "delegated"

    async def get_positions(self):
        return self.rows

    def get_balance(self):
        return 123


def test_contracts_to_base_roundtrip_for_ltc_multiplier():
    base = contracts_to_base(3, INFO)
    assert base == 0.3
    assert base_to_contracts(base, INFO) == 3


def test_position_adapter_converts_contract_size_to_base_and_preserves_raw_count():
    raw = {
        "symbol": "LTCUSDT",
        "side": "Buy",
        "size": 3.0,
        "entryPrice": 54.01,
    }
    client = FakeKuCoinClient(rows=[raw])
    adapter = KuCoinPositionUnitAdapter(client)

    rows = asyncio.run(adapter.get_positions())

    assert len(rows) == 1
    assert rows[0]["size"] == 0.3
    assert rows[0]["sizeContracts"] == 3.0
    assert rows[0]["sizeUnit"] == "BASE_ASSET"
    assert rows[0]["entryPrice"] == 54.01
    assert base_to_contracts(rows[0]["size"], INFO) == 3
    assert raw["size"] == 3.0
    assert "sizeContracts" not in raw


def test_position_adapter_delegates_non_position_client_api_unchanged():
    client = FakeKuCoinClient()
    adapter = KuCoinPositionUnitAdapter(client)
    assert adapter.marker == "delegated"
    assert adapter.get_balance() == 123


def test_position_adapter_fails_closed_without_instrument_metadata():
    client = FakeKuCoinClient(
        rows=[{"symbol": "LTCUSDT", "size": 3.0}],
        instruments={},
    )
    adapter = KuCoinPositionUnitAdapter(client)
    try:
        asyncio.run(adapter.get_positions())
    except RuntimeError as exc:
        assert "instrument metadata unavailable" in str(exc)
    else:
        raise AssertionError("missing contract multiplier must fail closed")


def test_position_adapter_fails_closed_for_unknown_symbol_metadata():
    client = FakeKuCoinClient(
        rows=[{"symbol": "LTCUSDT", "size": 3.0}],
        instruments={"BTCUSDT": dict(INFO)},
    )
    adapter = KuCoinPositionUnitAdapter(client)
    try:
        asyncio.run(adapter.get_positions())
    except RuntimeError as exc:
        assert "symbol=LTCUSDT" in str(exc)
    else:
        raise AssertionError("unknown symbol multiplier must fail closed")


def test_invalid_fractional_native_contract_count_is_rejected():
    try:
        contracts_to_base(3.5, INFO)
    except ValueError as exc:
        assert "lotSize" in str(exc)
    else:
        raise AssertionError("fractional native contract count must be rejected")


def test_runtime_engine_composes_position_unit_adapter_explicitly():
    source = inspect.getsource(nexus_runtime_engine.TradingEngine.__init__)
    assert "KuCoinPositionUnitAdapter" in source
    assert "self.client = KuCoinPositionUnitAdapter(self.client)" in source


def test_position_adapter_has_no_exchange_mutation_or_trading_policy_changes():
    source = inspect.getsource(__import__("bot.kucoin_position_units", fromlist=["x"]))
    forbidden = (
        "place_order",
        "cancel_order",
        "close_position",
        "set_position_stops",
        "set_leverage",
        "cfg.LEVERAGE",
        "MIN_ENTRY_SCORE",
        "MIN_VOLUME_MULT",
        "NEXUS_MIN_RR_NET",
    )
    assert all(token not in source for token in forbidden)
