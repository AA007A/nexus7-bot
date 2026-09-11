import unittest

from bot.pre_dispatch_guard import (
    MicrostructureLimits,
    evaluate_microstructure,
    live_microstructure_recheck,
    normalize_orderbook_base_units,
    recheck_exchange_exposure,
)


class FakeClient:
    def __init__(self, positions=None, active=None, fail_positions=False, fail_orders=False):
        self.positions = positions or []
        self.active = active if active is not None else {"items": []}
        self.fail_positions = fail_positions
        self.fail_orders = fail_orders

    async def get_positions(self):
        if self.fail_positions:
            raise RuntimeError("positions unavailable")
        return self.positions

    async def _get(self, endpoint, params=None, auth=False):
        if self.fail_orders:
            raise RuntimeError("orders unavailable")
        self.last_get = (endpoint, params, auth)
        return self.active


class FreshMarketClient:
    def __init__(self, ticker=None, book=None, fail=False):
        self.ticker = ticker or {"bid": 99.98, "ask": 100.02, "lastPrice": 100.0}
        self.book = book if book is not None else {
            "b": [[99.98, 50]],
            "a": [[100.02, 50]],
        }
        self.fail = fail
        self.ticker_reads = 0
        self.book_reads = 0

    async def get_ticker(self, symbol):
        self.ticker_reads += 1
        if self.fail:
            raise RuntimeError("ticker unavailable")
        return self.ticker

    async def get_orderbook(self, symbol, depth=20):
        self.book_reads += 1
        if self.fail:
            raise RuntimeError("book unavailable")
        return self.book


class PreDispatchGuardTests(unittest.IsolatedAsyncioTestCase):
    def test_good_microstructure_passes(self):
        out = evaluate_microstructure(
            signal_entry=100.0,
            side="Buy",
            qty=1.0,
            ticker={"bestBid": 99.98, "bestAsk": 100.02, "lastPrice": 100.0},
            orderbook={"asks": [[100.02, 2.0], [100.03, 2.0]]},
            limits=MicrostructureLimits(max_spread_bps=10, max_signal_drift_bps=10, min_depth_multiple=3),
        )
        self.assertTrue(out.allowed)

    def test_spread_drift_and_depth_fail_closed(self):
        out = evaluate_microstructure(
            signal_entry=100.0,
            side="Buy",
            qty=2.0,
            ticker={"bestBid": 99.0, "bestAsk": 101.0, "lastPrice": 100.0},
            orderbook={"asks": [[101.0, 1.0]]},
            limits=MicrostructureLimits(max_spread_bps=20, max_signal_drift_bps=30, min_depth_multiple=2),
        )
        self.assertFalse(out.allowed)
        self.assertIn("SPREAD_TOO_WIDE", out.blockers)
        self.assertIn("SIGNAL_PRICE_STALE", out.blockers)
        self.assertIn("INSUFFICIENT_BOOK_DEPTH", out.blockers)

    def test_missing_book_blocks(self):
        out = evaluate_microstructure(
            signal_entry=100.0,
            side="Sell",
            qty=1.0,
            ticker={"bestBid": 99.99, "bestAsk": 100.01},
            orderbook=None,
        )
        self.assertFalse(out.allowed)
        self.assertIn("ORDERBOOK_UNAVAILABLE", out.blockers)

    def test_orderbook_contract_depth_normalizes_to_base_units(self):
        out = normalize_orderbook_base_units(
            {"BTCUSDT": {"multiplier": 0.001}},
            "BTCUSDT",
            {"b": [[99.9, 2000]], "a": [[100.1, 3000]]},
        )
        self.assertEqual(out["bids"][0][1], 2.0)
        self.assertEqual(out["asks"][0][1], 3.0)

    async def test_live_recheck_uses_fresh_rest_reads_and_passes_good_market(self):
        c = FreshMarketClient()
        out = await live_microstructure_recheck(
            c,
            instruments={"BTCUSDT": {"multiplier": 0.1}},
            symbol="BTCUSDT",
            signal_entry=100.0,
            side="BUY",
            qty=1.0,
            limits=MicrostructureLimits(max_spread_bps=10, max_signal_drift_bps=10, min_depth_multiple=3),
        )
        self.assertTrue(out.allowed)
        self.assertEqual(c.ticker_reads, 1)
        self.assertEqual(c.book_reads, 1)
        self.assertGreaterEqual(out.metrics["depth_multiple"], 3.0)

    async def test_live_recheck_fails_closed_on_exchange_read_error(self):
        c = FreshMarketClient(fail=True)
        out = await live_microstructure_recheck(
            c,
            instruments={"BTCUSDT": {"multiplier": 0.1}},
            symbol="BTCUSDT",
            signal_entry=100.0,
            side="BUY",
            qty=1.0,
        )
        self.assertFalse(out.allowed)
        self.assertEqual(out.blockers, ["MICROSTRUCTURE_RECHECK_FAILED"])

    async def test_existing_symbol_position_blocks(self):
        c = FakeClient(positions=[{"symbol": "BTCUSDT", "size": 1}], active={"items": []})
        out = await recheck_exchange_exposure(c, "BTCUSDT")
        self.assertFalse(out.allowed)
        self.assertIn("SYMBOL_ALREADY_EXPOSED", out.blockers)

    async def test_any_active_order_blocks(self):
        c = FakeClient(active={"items": [{"symbol": "ETHUSDTM", "id": "1"}]})
        out = await recheck_exchange_exposure(c, "BTCUSDT")
        self.assertFalse(out.allowed)
        self.assertIn("ACTIVE_EXCHANGE_ORDER_PRESENT", out.blockers)
        self.assertEqual(c.last_get[0], "/api/v1/orders")
        self.assertTrue(c.last_get[2])

    async def test_read_failure_blocks(self):
        out = await recheck_exchange_exposure(FakeClient(fail_positions=True), "BTCUSDT")
        self.assertFalse(out.allowed)
        self.assertEqual(out.blockers, ["POSITION_RECHECK_FAILED"])


if __name__ == "__main__":
    unittest.main()
