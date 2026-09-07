import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import validation_safety_lock
from bot.engine import TradingEngine


class _Risk:
    def __init__(self):
        self.balance = 0.0
        self.drawdown = 0.0
        self._ready = False

    def init(self, balance):
        self.balance = float(balance)
        self._ready = True

    def update(self, balance):
        self.balance = float(balance)
        self.drawdown = 0.0


class _Client:
    async def _get(self, path, params=None, auth=False):
        assert path == "/api/v1/account-overview"
        assert auth is True
        return {
            "accountEquity": "20.0",
            "availableBalance": "0.4",
            "marginBalance": "19.5",
            "currency": "USDT",
        }


def _engine_for_update(paper_trade=False, locked=True):
    obj = object.__new__(TradingEngine)
    obj.paper_trade = paper_trade
    obj._validation_safety_lock_active = locked
    obj.risk = _Risk()
    obj.client = _Client()
    return obj


def test_patch_source_contains_no_exchange_mutation_calls():
    source = inspect.getsource(validation_safety_lock)
    forbidden = (
        ".place_order(", ".cancel_order(", ".close_position(",
        ".set_leverage(", ".set_sl(", ".set_position_stops(",
    )
    for token in forbidden:
        assert token not in source


def test_nonpaper_periodic_refresh_uses_equity_not_available():
    engine = _engine_for_update(False, True)
    asyncio.run(TradingEngine._update_balance(engine))
    assert engine.risk.balance == 20.0
    assert engine.risk.drawdown == 0.0
    assert engine._shadow_available_balance == 0.4


def test_nonpaper_without_validation_lock_fails_closed():
    engine = _engine_for_update(False, False)
    before = engine.risk.balance
    result = asyncio.run(TradingEngine._update_balance(engine))
    assert result is None
    assert engine.risk.balance == before


def test_paper_delegates_to_original_update(monkeypatch=None):
    # Static invariant: the wrapper explicitly delegates PAPER to the original
    # method rather than applying SHADOW-only account-equity semantics.
    source = inspect.getsource(validation_safety_lock)
    assert 'if getattr(self, "paper_trade", False):' in source
    assert 'return await original_update_balance(self, *args, **kwargs)' in source
