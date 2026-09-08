import unittest

from bot.account_capital_reader import read_account_capital


class _Client:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def _get(self, path, params=None, auth=False):
        self.calls.append((path, params, auth))
        return self.payload


class AccountCapitalReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_full_account_semantics(self):
        client = _Client({
            "accountEquity": "20",
            "availableBalance": "5",
            "positionMargin": "10",
            "orderMargin": "5",
            "unrealisedPNL": "-1",
        })
        snap = await read_account_capital(client)
        self.assertEqual(snap.capital.equity, 20.0)
        self.assertEqual(snap.capital.available_collateral, 5.0)
        self.assertEqual(snap.capital.position_margin, 10.0)
        self.assertEqual(snap.capital.order_margin, 5.0)
        self.assertEqual(snap.capital.unrealized_pnl, -1.0)
        self.assertEqual(client.calls, [(
            "/api/v1/account-overview", {"currency": "USDT"}, True
        )])

    async def test_unavailable_payload_fails_closed(self):
        client = _Client(None)
        with self.assertRaises(RuntimeError):
            await read_account_capital(client)

    async def test_negative_available_fails_closed(self):
        client = _Client({
            "accountEquity": 20,
            "availableBalance": -1,
        })
        with self.assertRaises(ValueError):
            await read_account_capital(client)


if __name__ == "__main__":
    unittest.main()
