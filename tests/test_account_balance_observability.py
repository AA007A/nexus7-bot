import asyncio
import unittest
from unittest.mock import AsyncMock

from bot import account_balance_observability as abo


class _Log:
    def __init__(self):
        self.rows = []

    def info(self, msg, *args):
        self.rows.append(("info", msg, args))

    def warning(self, msg, *args):
        self.rows.append(("warning", msg, args))


class AccountBalanceObservabilityTests(unittest.TestCase):
    def test_numeric_parser_is_fail_closed(self):
        self.assertEqual(abo._num({"x": "20.5"}, "x"), 20.5)
        self.assertIsNone(abo._num({"x": True}, "x"))
        self.assertIsNone(abo._num({"x": "nan"}, "x"))
        self.assertIsNone(abo._num({}, "x"))

    def test_snapshot_reads_only_account_overview(self):
        from bot.kucoin import KuCoinClient

        original = KuCoinClient.get_balance
        log = _Log()
        abo.install(log)
        try:
            c = KuCoinClient()
            c._get = AsyncMock(return_value={
                "currency": "USDT",
                "accountEquity": "20.0183",
                "marginBalance": "20.0183",
                "availableBalance": "0.0117",
                "unrealisedPNL": "0",
                "positionMargin": "20.0066",
                "orderMargin": "0",
                "frozenFunds": "0",
            })
            snap = asyncio.run(c.get_account_overview_snapshot())
            self.assertEqual(snap["accountEquity"], 20.0183)
            self.assertEqual(snap["availableBalance"], 0.0117)
            self.assertEqual(snap["positionMargin"], 20.0066)
            c._get.assert_awaited_once_with(
                "/api/v1/account-overview", {"currency": "USDT"}, auth=True
            )
        finally:
            KuCoinClient.get_balance = original
            if hasattr(KuCoinClient, "get_account_overview_snapshot"):
                delattr(KuCoinClient, "get_account_overview_snapshot")
            KuCoinClient._account_balance_observability_patched = False

    def test_module_contains_no_exchange_mutation_calls(self):
        import inspect
        src = inspect.getsource(abo)
        forbidden = (
            "place_order(", "cancel_all_orders(", "set_leverage(",
            "set_sl(", "set_position_stops(", "close_position(", "_post(",
        )
        for token in forbidden:
            self.assertNotIn(token, src)


if __name__ == "__main__":
    unittest.main()
