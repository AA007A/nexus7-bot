import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import account_balance_semantics as semantics


class _Risk:
    def __init__(self):
        self._ready = False
        self.balance = 0.0
        self.peak_balance = 0.0
        self.drawdown = 0.0
        self.init_calls = 0
        self.update_calls = 0

    def init(self, equity):
        self.init_calls += 1
        self._ready = True
        self.balance = equity
        self.peak_balance = max(self.peak_balance, equity)
        self.drawdown = 0.0

    def update(self, equity):
        self.update_calls += 1
        self.balance = equity
        self.peak_balance = max(self.peak_balance, equity)
        self.drawdown = (
            (self.peak_balance - equity) / self.peak_balance
            if self.peak_balance > 0 else 0.0
        )


class AccountBalanceSemanticsSharedTests(unittest.IsolatedAsyncioTestCase):
    async def test_equity_and_available_are_separate(self):
        client = SimpleNamespace(_get=AsyncMock(return_value={
            "currency": "USDT",
            "accountEquity": "20.0",
            "marginBalance": "20.0",
            "availableBalance": "0.4",
            "unrealisedPNL": "0.0",
            "positionMargin": "19.6",
            "orderMargin": "0.0",
            "frozenFunds": "0.0",
        }))
        state = await semantics.read_account_state(client)
        self.assertEqual(state["equity"], 20.0)
        self.assertEqual(state["available"], 0.4)
        client._get.assert_awaited_once_with(
            "/api/v1/account-overview", {"currency": "USDT"}, auth=True
        )

    async def test_invalid_or_negative_balances_fail_closed(self):
        bad_payloads = (
            {"accountEquity": None, "availableBalance": "1"},
            {"accountEquity": "nan", "availableBalance": "1"},
            {"accountEquity": "10", "availableBalance": "-1"},
        )
        for payload in bad_payloads:
            client = SimpleNamespace(_get=AsyncMock(return_value=payload))
            with self.assertRaises((ValueError, RuntimeError, TypeError)):
                await semantics.read_account_state(client)

    async def test_risk_drawdown_uses_equity_not_available(self):
        risk = _Risk()
        semantics.update_risk_from_equity(risk, 20.0)
        semantics.update_risk_from_equity(risk, 18.0)
        self.assertEqual(risk.balance, 18.0)
        self.assertAlmostEqual(risk.drawdown, 0.10)
        self.assertEqual(risk.init_calls, 1)
        self.assertEqual(risk.update_calls, 1)

    async def test_available_collateral_still_blocks_affordability(self):
        allowed, required = semantics.collateral_allows(
            qty=1.0, entry=100.0, available=0.4, leverage=10, fee_rate=0.0006
        )
        self.assertFalse(allowed)
        self.assertGreater(required, 0.4)

    async def test_module_contains_no_exchange_mutation_calls(self):
        import inspect
        source = inspect.getsource(semantics)
        forbidden = (
            ".place_order(",
            ".cancel_order(",
            ".cancel_all_orders(",
            ".close_position(",
            ".set_leverage(",
            ".set_sl(",
            ".set_position_stops(",
        )
        for token in forbidden:
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
