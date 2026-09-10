import unittest

from bot import account_balance_semantics as sem


class _Client:
    def __init__(self, payload):
        self.payload = payload

    async def _get(self, path, params=None, auth=False):
        self.last = (path, params, auth)
        return dict(self.payload)


class AccountBalanceSemanticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefers_available_margin_for_cross_margin(self):
        client = _Client({
            "accountEquity": "100",
            "marginBalance": "95",
            "availableBalance": "12",
            "availableMargin": "44",
            "unrealisedPNL": "5",
            "positionMargin": "40",
            "orderMargin": "0",
            "frozenFunds": "0",
            "currency": "USDT",
        })
        state = await sem.read_account_state(client)
        self.assertEqual(state["equity"], 100.0)
        self.assertEqual(state["available"], 44.0)
        self.assertEqual(state["available_source"], "availableMargin")
        self.assertEqual(client.last, (
            "/api/v1/account-overview", {"currency": "USDT"}, True
        ))

    async def test_falls_back_to_available_balance_when_margin_absent(self):
        client = _Client({
            "accountEquity": "100",
            "marginBalance": "100",
            "availableBalance": "27.5",
            "unrealisedPNL": "0",
            "positionMargin": "0",
            "orderMargin": "0",
            "frozenFunds": "0",
            "currency": "USDT",
        })
        state = await sem.read_account_state(client)
        self.assertEqual(state["available"], 27.5)
        self.assertEqual(state["available_source"], "availableBalance")

    async def test_drawdown_capital_basis_remains_account_equity(self):
        client = _Client({
            "accountEquity": "36",
            "marginBalance": "74",
            "availableBalance": "14",
            "availableMargin": "18",
            "unrealisedPNL": "-38",
            "positionMargin": "20",
            "orderMargin": "0",
            "frozenFunds": "0",
            "currency": "USDT",
        })
        state = await sem.read_account_state(client)
        self.assertEqual(state["equity"], 36.0)
        self.assertEqual(state["available"], 18.0)

    async def test_invalid_available_margin_fails_closed(self):
        client = _Client({
            "accountEquity": "100",
            "availableBalance": "20",
            "availableMargin": "nan",
        })
        with self.assertRaises(ValueError):
            await sem.read_account_state(client)


if __name__ == "__main__":
    unittest.main()
