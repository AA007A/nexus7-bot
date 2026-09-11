import asyncio
import unittest

from bot import pilot_risk_cap_hardening as guard


class PilotRiskCapPureTests(unittest.TestCase):
    def test_target_used_when_below_risk_limit(self):
        self.assertEqual(
            guard._select_final_quantity(target_qty=10.0, risk_qty=15.0),
            10.0,
        )

    def test_risk_limit_overrides_50pct_target_when_lower(self):
        self.assertEqual(
            guard._select_final_quantity(target_qty=10.0, risk_qty=6.5),
            6.5,
        )

    def test_never_exceeds_either_cap(self):
        for target, risk in ((10.0, 20.0), (20.0, 10.0), (7.25, 7.25)):
            final = guard._select_final_quantity(target_qty=target, risk_qty=risk)
            self.assertLessEqual(final, target)
            self.assertLessEqual(final, risk)

    def test_invalid_inputs_fail_closed(self):
        self.assertEqual(guard._select_final_quantity(target_qty=0, risk_qty=10), 0.0)
        self.assertEqual(guard._select_final_quantity(target_qty=10, risk_qty=0), 0.0)
        self.assertEqual(guard._select_final_quantity(target_qty=float("nan"), risk_qty=10), 0.0)
        self.assertEqual(guard._select_final_quantity(target_qty=10, risk_qty=float("inf")), 0.0)


class _Pilot:
    enabled = True


class _Risk:
    def __init__(self, qty):
        self.qty = qty
        self.calls = []

    def size(self, symbol, entry, instruments, size_mult=1.0, open_positions=None):
        self.calls.append((symbol, entry, instruments, open_positions))
        return self.qty


class _Log:
    def critical(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


class PilotRiskCapInstallTests(unittest.TestCase):
    def test_live_pilot_hook_caps_target_with_risk_quantity(self):
        from bot import engine as engine_module

        original_module_minimum = engine_module.minimum_base_quantity

        class FakeEngine:
            _pilot_risk_cap_hardening_installed = False

            def __init__(self):
                self.paper_trade = False
                self.pilot = _Pilot()
                self.risk = _Risk(6.0)
                self.instruments = {"DOTUSDT": {"multiplier": 1}}
                self.positions = {}
                self.observed_qty = None

            async def _open(self, sig):
                self.observed_qty = engine_module.minimum_base_quantity(
                    self.instruments["DOTUSDT"], 1.0
                )
                return self.observed_qty

        class Sig:
            symbol = "DOTUSDT"

        try:
            # Emulate the already-installed pilot 50%-notional hook returning 10.
            engine_module.minimum_base_quantity = lambda info, price: 10.0
            guard.install(FakeEngine, _Log())
            instance = FakeEngine()
            result = asyncio.run(instance._open(Sig()))

            self.assertEqual(result, 6.0)
            self.assertEqual(instance.observed_qty, 6.0)
            self.assertEqual(len(instance.risk.calls), 1)
            self.assertEqual(instance.risk.calls[0][0], "DOTUSDT")
        finally:
            engine_module.minimum_base_quantity = original_module_minimum


if __name__ == "__main__":
    unittest.main()
