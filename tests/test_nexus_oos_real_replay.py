import unittest

from bot.nexus_oos_real_replay import _simulate_net_r


class TestNexusOOSRealReplay(unittest.TestCase):
    def _candles(self, price=100.0, n=50):
        out = []
        for i in range(n):
            out.append({
                "ts": 1_700_000_000_000 + i * 900_000,
                "o": price,
                "h": price,
                "l": price,
                "c": price,
                "v": 1.0,
            })
        return out

    def test_long_take_profit_net_r_positive_after_costs(self):
        candles = self._candles()
        candles[0].update({"o": 100.0, "h": 103.0, "l": 99.5, "c": 102.0})
        r = _simulate_net_r(
            direction="LONG",
            signal_entry=100.0,
            signal_sl=99.0,
            signal_tp=102.0,
            signal_tp1=102.0,
            signal_tp2=102.0,
            decision_idx=0,
            decision_ts=candles[0]["ts"],
            klines_15=candles,
            funding_events=[],
            fee_rate=0.0006,
            slippage_rate=0.0005,
        )
        self.assertIsNotNone(r)
        self.assertGreater(r, 0.0)

    def test_same_bar_stop_first_is_conservative(self):
        candles = self._candles()
        candles[0].update({"o": 100.0, "h": 103.0, "l": 98.0, "c": 100.0})
        r = _simulate_net_r(
            direction="LONG",
            signal_entry=100.0,
            signal_sl=99.0,
            signal_tp=102.0,
            signal_tp1=102.0,
            signal_tp2=102.0,
            decision_idx=0,
            decision_ts=candles[0]["ts"],
            klines_15=candles,
            funding_events=[],
            fee_rate=0.0,
            slippage_rate=0.0,
        )
        self.assertAlmostEqual(r, -1.0, places=6)

    def test_short_take_profit_net_r_positive(self):
        candles = self._candles()
        candles[0].update({"o": 100.0, "h": 100.5, "l": 97.0, "c": 98.0})
        r = _simulate_net_r(
            direction="SHORT",
            signal_entry=100.0,
            signal_sl=101.0,
            signal_tp=98.0,
            signal_tp1=98.0,
            signal_tp2=98.0,
            decision_idx=0,
            decision_ts=candles[0]["ts"],
            klines_15=candles,
            funding_events=[],
            fee_rate=0.0006,
            slippage_rate=0.0005,
        )
        self.assertIsNotNone(r)
        self.assertGreater(r, 0.0)


if __name__ == "__main__":
    unittest.main()
