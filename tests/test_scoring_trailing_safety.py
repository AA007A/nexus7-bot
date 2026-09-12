import unittest
from types import SimpleNamespace

import numpy as np

from bot import scoring_safety_hardening
from bot import trailing_safety_hardening


class _Log:
    def warning(self, *args, **kwargs):
        pass


class ScoringSafetyTests(unittest.TestCase):
    def _strategy(self, *, adx_direction="LONG", fail_optional=False):
        def ema(values, period):
            value = 100.0 if period == 20 else (90.0 if period == 50 else 80.0)
            return np.array([value] * len(values), dtype=float)

        def adx_fn(*_args):
            return {"adx": 35.0, "direction": adx_direction}

        def smc_analysis(*_args):
            return {
                "structure": "NEUTRAL", "hh": False, "hl": False,
                "lh": False, "ll": False, "bos": False,
                "bos_dir": "NONE", "choch": False,
            }

        def optional(*_args, **_kwargs):
            if fail_optional:
                raise RuntimeError("missing optional source")
            return 100.0

        def volume_profile(*_args):
            if fail_optional:
                raise RuntimeError("missing VP")
            return {"poc": 100.0}

        def footprint(*_args):
            if fail_optional:
                raise RuntimeError("missing footprint")
            return {"bias": "NEUTRAL", "divergence": False}

        return SimpleNamespace(
            np=np,
            adx_fn=adx_fn,
            ema=ema,
            smc_analysis=smc_analysis,
            vwap_fn=optional,
            orderbook_imbalance=lambda *_: {"bias": "NEUTRAL"},
            volume_profile=volume_profile,
            rsi=lambda c: np.array([50.0] * len(c)),
            macd=lambda c: (None, None, np.zeros(len(c))),
            delta_footprint=footprint,
            bollinger=lambda c: {"squeezed": False, "width": 3.0},
            chop_fn=lambda *a: {"chop": False, "trending": True, "ci": 40.0},
        )

    def _inputs(self, *, doji=False):
        closes = [101.0] * 30
        highs = [102.0] * 30
        lows = [99.0] * 30
        opens = [101.0 if doji else 100.0] * 30
        volumes = [10.0] * 29 + [30.0]
        return closes, highs, lows, opens, volumes

    def test_optional_indicator_failures_never_award_credit(self):
        strategy = self._strategy(fail_optional=True)
        scoring_safety_hardening.install(strategy, _Log())
        data = self._inputs()
        result = strategy.score_tf(*data, "LONG", 1.0, 1.0)
        self.assertFalse(result["vwap_ok"])
        self.assertFalse(result["fp_ok"])
        # No orderbook/POC bonus: directional high-volume bodies cap naturally at 20.
        self.assertLessEqual(result["vol_s"], 20)

    def test_adx_points_require_adx_direction_alignment(self):
        strategy = self._strategy(adx_direction="SHORT", fail_optional=True)
        scoring_safety_hardening.install(strategy, _Log())
        data = self._inputs()
        result = strategy.score_tf(*data, "LONG", 1.0, 1.0)
        self.assertFalse(result["adx_aligned"])
        # Full EMA stack contributes 10; opposite ADX must contribute zero.
        self.assertEqual(result["trend_s"], 10)

    def test_doji_is_not_directional_short_volume_body(self):
        strategy = self._strategy(adx_direction="SHORT", fail_optional=True)
        scoring_safety_hardening.install(strategy, _Log())
        data = self._inputs(doji=True)
        result = strategy.score_tf(*data, "SHORT", 1.0, 1.0)
        # vol_r is high, but three dojis must not satisfy all SHORT bodies.
        self.assertEqual(result["vol_s"], 9)


class TrailingSafetyTests(unittest.TestCase):
    def _position_class(self):
        class Position:
            pass
        return Position

    def test_long_trailing_retains_75_percent_of_peak_excursion(self):
        Position = self._position_class()
        cfg = SimpleNamespace(TRAILING_TRIGGER=0.50, TRAILING_LOCK=0.25)
        trailing_safety_hardening.install(Position, cfg, _Log())
        p = Position()
        p.pnl = 20.0
        p.peak_pnl = 20.0
        p.qty = 10.0
        p.entry = 100.0
        p.tp = 104.0
        p.sl = 98.0
        p.direction = "LONG"
        p.trailing_active = False
        self.assertEqual(p.calc_trailing_sl(), 101.5)
        self.assertTrue(p.trailing_active)

    def test_short_trailing_retains_75_percent_of_peak_excursion(self):
        Position = self._position_class()
        cfg = SimpleNamespace(TRAILING_TRIGGER=0.50, TRAILING_LOCK=0.25)
        trailing_safety_hardening.install(Position, cfg, _Log())
        p = Position()
        p.pnl = 20.0
        p.peak_pnl = 20.0
        p.qty = 10.0
        p.entry = 100.0
        p.tp = 96.0
        p.sl = 102.0
        p.direction = "SHORT"
        p.trailing_active = False
        self.assertEqual(p.calc_trailing_sl(), 98.5)
        self.assertTrue(p.trailing_active)


if __name__ == "__main__":
    unittest.main()
