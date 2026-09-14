import asyncio
from types import SimpleNamespace
import unittest

from bot import operator_loss_policy as policy


class _Log:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _Engine:
    paper_trade = False

    def __init__(self):
        self.pilot = SimpleNamespace(enabled=True)

    async def _open(self, sig, *args, **kwargs):
        return sig

    async def _check_stagnation_and_invalidation(self):
        return "legacy-exit"


class OperatorTechnicalStopPolicyTests(unittest.TestCase):
    def test_validate_long_technical_geometry(self):
        result = policy.validate_technical_geometry(
            100.0, 98.0, 105.0, "LONG", 0.0022, 1.2
        )
        self.assertAlmostEqual(result["stop_distance_pct"], 2.0)
        self.assertAlmostEqual(result["target_distance_pct"], 5.0)
        self.assertGreater(result["estimated_net_rr"], 1.2)

    def test_validate_short_technical_geometry(self):
        result = policy.validate_technical_geometry(
            100.0, 102.0, 95.0, "SHORT", 0.0022, 1.2
        )
        self.assertAlmostEqual(result["stop_distance_pct"], 2.0)
        self.assertAlmostEqual(result["target_distance_pct"], 5.0)
        self.assertGreater(result["estimated_net_rr"], 1.2)

    def test_invalid_directional_geometry_fails_closed(self):
        with self.assertRaises(ValueError):
            policy.validate_technical_geometry(
                100.0, 101.0, 105.0, "LONG", 0.0022, 1.2
            )

    def test_net_rr_below_minimum_fails_closed(self):
        with self.assertRaises(ValueError):
            policy.validate_technical_geometry(
                100.0, 98.0, 101.0, "LONG", 0.0022, 1.2
            )

    def test_live_wrapper_preserves_sl_tp_tp1_tp2_exactly(self):
        class Engine(_Engine):
            pass

        policy.install(Engine, _Log())
        engine = Engine()
        sig = SimpleNamespace(
            symbol="BTCUSDT",
            direction="LONG",
            entry=100.0,
            sl=98.0,
            tp=105.0,
            tp1=103.0,
            tp2=105.0,
        )
        before = (sig.sl, sig.tp, sig.tp1, sig.tp2)
        result = asyncio.run(engine._open(sig))
        after = (sig.sl, sig.tp, sig.tp1, sig.tp2)

        self.assertIs(result, sig)
        self.assertEqual(after, before)

    def test_live_wrapper_blocks_bad_geometry_without_mutation(self):
        class Engine(_Engine):
            pass

        policy.install(Engine, _Log())
        engine = Engine()
        sig = SimpleNamespace(
            symbol="BTCUSDT",
            direction="LONG",
            entry=100.0,
            sl=101.0,
            tp=105.0,
            tp1=103.0,
            tp2=105.0,
        )
        before = (sig.sl, sig.tp, sig.tp1, sig.tp2)
        result = asyncio.run(engine._open(sig))
        after = (sig.sl, sig.tp, sig.tp1, sig.tp2)

        self.assertIsNone(result)
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
