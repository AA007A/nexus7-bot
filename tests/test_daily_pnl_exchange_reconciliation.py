import unittest
from datetime import datetime, timezone

from bot import durable_daily_pnl as pnl


class DailyPnlExchangeReconciliationTests(unittest.TestCase):
    def test_confirmed_kucoin_pnl_adjusts_estimate_exactly_once(self):
        rows = {
            "a" * 64: {
                "pnl": -2.33165996,
                "source": "ESTIMATED_LOCAL_MARK_AND_FEE_RATE",
                "closed_at": "2026-09-14T00:33:47+00:00",
            }
        }
        row = {
            "closeId": "200000000043135568",
            "symbol": "LINKUSDTM",
            "closeTime": int(datetime(2026, 9, 14, 0, 33, 41, 700000, tzinfo=timezone.utc).timestamp() * 1000),
            "pnl": "-3.0559949",
        }

        result = pnl._confirmed_adjustment(rows, row)
        self.assertIsNotNone(result)
        token, value = result
        self.assertEqual(len(token), 64)
        self.assertAlmostEqual(value["estimated_pnl"], -2.33165996)
        self.assertAlmostEqual(value["confirmed_pnl"], -3.0559949)
        self.assertAlmostEqual(value["pnl"], -0.72433494)
        self.assertEqual(value["source"], "KUCOIN_RECONCILIATION_ADJUSTMENT")
        self.assertEqual(value["symbol"], "LINKUSDT")

        total = sum(v["pnl"] for v in rows.values()) + value["pnl"]
        self.assertAlmostEqual(total, -3.0559949)

        # Same closeId always derives the same adjustment identity/payload.
        again = pnl._confirmed_adjustment(rows, row)
        self.assertEqual(result, again)

    def test_ambiguous_estimates_fail_closed_without_adjustment(self):
        rows = {
            "a" * 64: {
                "pnl": -1.0,
                "source": "ESTIMATED_LOCAL_MARK_AND_FEE_RATE",
                "closed_at": "2026-09-14T00:33:40+00:00",
            },
            "b" * 64: {
                "pnl": -2.0,
                "source": "ESTIMATED_LOCAL_MARK_AND_FEE_RATE",
                "closed_at": "2026-09-14T00:33:42+00:00",
            },
        }
        row = {
            "closeId": "close-1",
            "symbol": "LINKUSDTM",
            "closeTime": int(datetime(2026, 9, 14, 0, 33, 41, tzinfo=timezone.utc).timestamp() * 1000),
            "pnl": "-3.0",
        }
        self.assertIsNone(pnl._confirmed_adjustment(rows, row))

    def test_symbol_metadata_prevents_wrong_trade_match(self):
        rows = {
            "a" * 64: {
                "pnl": -1.0,
                "source": "ESTIMATED_LOCAL_MARK_AND_FEE_RATE",
                "closed_at": "2026-09-14T00:33:41+00:00",
                "symbol": "ETHUSDT",
            }
        }
        row = {
            "closeId": "close-2",
            "symbol": "LINKUSDTM",
            "closeTime": int(datetime(2026, 9, 14, 0, 33, 41, tzinfo=timezone.utc).timestamp() * 1000),
            "pnl": "-3.0",
        }
        self.assertIsNone(pnl._confirmed_adjustment(rows, row))

    def test_non_estimated_event_is_never_rewritten(self):
        rows = {
            "a" * 64: {
                "pnl": -1.0,
                "source": "KUCOIN_RECONCILED_FILLS",
                "closed_at": "2026-09-14T00:33:41+00:00",
                "symbol": "LINKUSDT",
            }
        }
        row = {
            "closeId": "close-3",
            "symbol": "LINKUSDTM",
            "closeTime": int(datetime(2026, 9, 14, 0, 33, 41, tzinfo=timezone.utc).timestamp() * 1000),
            "pnl": "-3.0",
        }
        self.assertIsNone(pnl._confirmed_adjustment(rows, row))


if __name__ == "__main__":
    unittest.main()
