import unittest

from bot.core_execution_risk import build_execution_risk_plan, final_read_only_dispatch_recheck


class _Client:
    def __init__(self, positions=None, orders=None, fail_positions=False, fail_orders=False):
        self.positions = positions or []
        self.orders = orders or []
        self.fail_positions = fail_positions
        self.fail_orders = fail_orders

    async def get_positions(self):
        if self.fail_positions:
            raise RuntimeError("position read failed")
        return list(self.positions)

    async def _get(self, path, params=None, auth=False):
        assert path == "/api/v1/orders"
        assert params == {"status": "active"}
        assert auth is True
        if self.fail_orders:
            raise RuntimeError("order read failed")
        return {"items": list(self.orders)}


class CoreExecutionRiskTests(unittest.IsolatedAsyncioTestCase):
    def _plan(self, **overrides):
        args = dict(
            account_overview={
                "accountEquity": 1000,
                "availableBalance": 200,
                "positionMargin": 700,
                "orderMargin": 100,
                "unrealisedPNL": -10,
            },
            entry=100.0,
            stop=98.0,
            side="BUY",
            risk_pct=0.01,
            leverage=10,
            qty_step=0.01,
            min_qty=0.01,
            max_margin_pct=0.50,
            fee_rate_per_side=0.0006,
            expected_slippage_pct=0.001,
            ticker={"bestBid": 99.99, "bestAsk": 100.01, "lastPrice": 100.0},
            orderbook={"asks": [[100.01, 100]], "bids": [[99.99, 100]]},
        )
        args.update(overrides)
        return build_execution_risk_plan(**args)

    def test_equity_sets_risk_budget_available_sets_margin_cap(self):
        plan = self._plan()
        self.assertAlmostEqual(plan.sizing.risk_budget, 10.0)
        self.assertLessEqual(plan.sizing.required_margin, 100.0 + 1e-9)
        self.assertEqual(plan.capital.equity, 1000.0)
        self.assertEqual(plan.capital.available_collateral, 200.0)
        self.assertEqual(plan.capital.position_margin, 700.0)
        self.assertEqual(plan.capital.order_margin, 100.0)
        self.assertTrue(plan.allowed)

    def test_available_collateral_never_replaces_equity_risk_budget(self):
        plan = self._plan(account_overview={
            "accountEquity": 1000,
            "availableBalance": 10,
            "positionMargin": 990,
            "orderMargin": 0,
        })
        self.assertAlmostEqual(plan.sizing.risk_budget, 10.0)
        self.assertLessEqual(plan.sizing.required_margin, 5.0 + 1e-9)
        self.assertEqual(plan.sizing.binding_constraint, "AVAILABLE_COLLATERAL")

    def test_wide_spread_blocks_even_when_sizing_is_valid(self):
        plan = self._plan(ticker={"bestBid": 99.0, "bestAsk": 101.0, "lastPrice": 100.0})
        self.assertGreater(plan.sizing.qty, 0)
        self.assertFalse(plan.allowed)
        self.assertIn("SPREAD_TOO_WIDE", plan.microstructure.blockers)

    def test_shallow_book_blocks(self):
        plan = self._plan(orderbook={"asks": [[100.01, 0.01]], "bids": [[99.99, 0.01]]})
        self.assertFalse(plan.allowed)
        self.assertIn("INSUFFICIENT_BOOK_DEPTH", plan.microstructure.blockers)

    async def test_final_recheck_clear(self):
        result = await final_read_only_dispatch_recheck(_Client(), "ATOMUSDT")
        self.assertTrue(result.allowed)

    async def test_final_recheck_blocks_active_order(self):
        result = await final_read_only_dispatch_recheck(_Client(orders=[{"id": "x"}]), "ATOMUSDT")
        self.assertFalse(result.allowed)
        self.assertIn("ACTIVE_EXCHANGE_ORDER_PRESENT", result.blockers)

    async def test_final_recheck_fail_closed_on_read_error(self):
        result = await final_read_only_dispatch_recheck(_Client(fail_positions=True), "ATOMUSDT")
        self.assertFalse(result.allowed)
        self.assertIn("POSITION_RECHECK_FAILED", result.blockers)


if __name__ == "__main__":
    unittest.main()
