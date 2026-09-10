import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.risk import RiskManager
from bot import shadow_balance_semantics as semantics


class _Client:
    def __init__(self, equity="20.0"):
        self.equity = equity

    async def _get(self, path, params=None, auth=False):
        assert path == "/api/v1/account-overview"
        assert auth is True
        return {
            "currency": "USDT",
            "accountEquity": self.equity,
            "marginBalance": "19.5",
            "availableBalance": "0.4",
            "unrealisedPNL": "0.5",
            "positionMargin": "19.6",
            "orderMargin": "0",
            "frozenFunds": "0",
        }


def test_equity_not_available_drives_drawdown():
    risk = RiskManager()
    risk.init(20.0)
    engine = SimpleNamespace(client=_Client(), risk=risk)

    with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value=None)), \
         patch("bot.drawdown_persistence.db.save_key_value", AsyncMock(return_value=True)):
        state = asyncio.run(semantics.refresh_shadow_risk(engine))

    assert state["equity"] == 20.0
    assert state["available"] == 0.4
    assert risk.balance == 20.0
    assert risk.drawdown == 0.0
    assert risk.peak_balance == 20.0
    assert engine._shadow_available_balance == 0.4


def test_restart_restores_durable_peak_in_shadow():
    risk = RiskManager()
    risk.init(15.0)
    engine = SimpleNamespace(client=_Client(equity="15.0"), risk=risk)

    with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value="20.0")), \
         patch("bot.drawdown_persistence.db.save_key_value", AsyncMock(return_value=True)) as save:
        asyncio.run(semantics.refresh_shadow_risk(engine))

    assert risk.peak_balance == 20.0
    assert risk.drawdown == 0.25
    save.assert_not_awaited()


def test_low_available_still_blocks_new_hypothetical_order():
    ok, required = semantics.collateral_allows(
        qty=1.0, entry=10.0, available=0.4, leverage=10, fee_rate=0.0006
    )
    assert ok is False
    assert required > 0.4


def test_sufficient_available_allows_collateral_gate():
    ok, required = semantics.collateral_allows(
        qty=0.1, entry=10.0, available=1.0, leverage=10, fee_rate=0.0006
    )
    assert ok is True
    assert required <= 1.0


def test_module_is_read_only():
    source = inspect.getsource(semantics)
    forbidden = (
        ".place_order(", ".set_sl(", ".set_leverage(",
        ".cancel_order(", ".close_position(", "reduce_only=True",
    )
    for token in forbidden:
        assert token not in source
