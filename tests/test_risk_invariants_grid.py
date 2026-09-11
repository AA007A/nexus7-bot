import unittest

from bot.professional_risk import CapitalState, stop_risk_size


class StopRiskInvariantGridTests(unittest.TestCase):
    """Deterministic property-style coverage for the core sizing invariants."""

    def test_projected_stop_loss_never_exceeds_risk_budget(self):
        equities = (10.0, 20.8133, 100.0, 1000.0)
        available_fracs = (0.10, 0.50, 1.00)
        stop_pcts = (0.0025, 0.005, 0.01, 0.02, 0.05)
        leverages = (1.0, 5.0, 10.0, 20.0, 50.0)
        prices = (0.10, 1.16, 10.0, 100.0, 1000.0, 100000.0)

        for equity in equities:
            for available_frac in available_fracs:
                capital = CapitalState(
                    equity=equity,
                    available_collateral=equity * available_frac,
                )
                for entry in prices:
                    for stop_pct in stop_pcts:
                        for direction in ("LONG", "SHORT"):
                            stop = entry * (1.0 - stop_pct) if direction == "LONG" else entry * (1.0 + stop_pct)
                            for leverage in leverages:
                                with self.subTest(
                                    equity=equity,
                                    available_frac=available_frac,
                                    entry=entry,
                                    stop_pct=stop_pct,
                                    direction=direction,
                                    leverage=leverage,
                                ):
                                    result = stop_risk_size(
                                        capital=capital,
                                        entry=entry,
                                        stop=stop,
                                        risk_pct=0.01,
                                        leverage=leverage,
                                        qty_step=1e-6,
                                        min_qty=1e-6,
                                        max_margin_pct=0.80,
                                        fee_rate_per_side=0.0006,
                                        expected_slippage_pct=0.0005,
                                    )
                                    self.assertLessEqual(result.projected_stop_loss, result.risk_budget * 1.000001)
                                    self.assertLessEqual(result.required_margin, capital.available_collateral * 0.80 * 1.000001)
                                    self.assertGreaterEqual(result.qty, 0.0)
                                    self.assertGreaterEqual(result.notional, 0.0)

    def test_higher_leverage_never_increases_stop_risk_budget(self):
        capital = CapitalState(equity=100.0, available_collateral=100.0)
        results = []
        for leverage in (1.0, 5.0, 10.0, 20.0, 50.0):
            result = stop_risk_size(
                capital=capital,
                entry=100.0,
                stop=98.0,
                risk_pct=0.01,
                leverage=leverage,
                qty_step=0.001,
                min_qty=0.001,
                max_margin_pct=0.80,
                fee_rate_per_side=0.0006,
                expected_slippage_pct=0.0005,
            )
            results.append(result)
            self.assertAlmostEqual(result.risk_budget, 1.0, places=9)
            self.assertLessEqual(result.projected_stop_loss, 1.000001)
        self.assertTrue(all(r.risk_budget == results[0].risk_budget for r in results))

    def test_exchange_minimum_fails_closed_when_it_cannot_fit_risk(self):
        result = stop_risk_size(
            capital=CapitalState(equity=20.0, available_collateral=20.0),
            entry=100.0,
            stop=95.0,
            risk_pct=0.01,
            leverage=50.0,
            qty_step=1.0,
            min_qty=1.0,
            max_margin_pct=0.80,
            fee_rate_per_side=0.0006,
            expected_slippage_pct=0.0005,
        )
        self.assertEqual(result.qty, 0.0)
        self.assertEqual(result.notional, 0.0)
        self.assertEqual(result.binding_constraint, "MINIMUM_ORDER")


if __name__ == "__main__":
    unittest.main()
