import asyncio

from bot import market_viability_fail_closed


class _Cfg:
    SYMBOLS = ["AVAXUSDT", "BTCUSDT"]


class _Log:
    def info(self, *args, **kwargs):
        pass
    def warning(self, *args, **kwargs):
        pass
    def critical(self, *args, **kwargs):
        pass


class _Client:
    def __init__(self, tickers=None, cached=None, raise_tickers=False):
        self._tickers = tickers or []
        self._cached = cached or {}
        self._raise = raise_tickers

    async def get_all_tickers(self):
        if self._raise:
            raise RuntimeError("ticker down")
        return self._tickers

    def get_cached_ticker(self, symbol):
        return self._cached.get(symbol, {})


def _engine_class(original_result=True, original_symbols=None):
    class Engine:
        async def _ensure_viable_symbols(self):
            self.viable_symbols = list(original_symbols or _Cfg.SYMBOLS)
            return original_result
    market_viability_fail_closed.install(Engine, _Cfg, _Log())
    return Engine


def test_all_unverified_symbols_fail_closed():
    Engine = _engine_class()
    eng = Engine()
    eng.client = _Client(tickers=[])
    eng.instruments = {
        "AVAXUSDT": {"multiplier": 0.1, "minQty": 1},
        "BTCUSDT": {"multiplier": 0.001, "minQty": 1},
    }
    assert asyncio.run(eng._ensure_viable_symbols()) is False
    assert eng.viable_symbols == []


def test_only_independently_verified_symbols_remain():
    Engine = _engine_class()
    eng = Engine()
    eng.client = _Client(tickers=[{"symbol": "AVAXUSDT", "lastPrice": "7.4"}])
    eng.instruments = {
        "AVAXUSDT": {"multiplier": 0.1, "minQty": 1},
        "BTCUSDT": {"multiplier": 0.001, "minQty": 1},
    }
    assert asyncio.run(eng._ensure_viable_symbols()) is True
    assert eng.viable_symbols == ["AVAXUSDT"]


def test_ws_cache_can_verify_when_rest_tickers_fail():
    Engine = _engine_class()
    eng = Engine()
    eng.client = _Client(
        raise_tickers=True,
        cached={"BTCUSDT": {"lastPrice": "115000"}},
    )
    eng.instruments = {
        "BTCUSDT": {"multiplier": 0.001, "minQty": 1},
    }
    eng.viable_symbols = ["BTCUSDT"]
    assert asyncio.run(eng._ensure_viable_symbols()) is True
    assert eng.viable_symbols == ["BTCUSDT"]


def test_invalid_instrument_metadata_is_removed():
    Engine = _engine_class(original_symbols=["AVAXUSDT"])
    eng = Engine()
    eng.client = _Client(tickers=[{"symbol": "AVAXUSDT", "lastPrice": "7.4"}])
    eng.instruments = {"AVAXUSDT": {"multiplier": 0, "minQty": 1}}
    assert asyncio.run(eng._ensure_viable_symbols()) is False
    assert eng.viable_symbols == []
