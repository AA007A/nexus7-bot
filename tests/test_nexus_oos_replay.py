import unittest
from types import SimpleNamespace

from bot.nexus_oos_replay import (
    _candidate_r_multiple,
    build_nexus_oos_candidates,
    clock_normalized_windows,
)


def _candles(count, interval_min, end_open_ms, price=100.0):
    step = interval_min * 60 * 1000
    start = end_open_ms - (count - 1) * step
    return [
        {
            "ts": start + i * step,
            "o": price,
            "h": price + 1.0,
            "l": price - 1.0,
            "c": price,
            "v": 10.0,
        }
        for i in range(count)
    ]


class NexusOOSReplayTests(unittest.TestCase):
    def test_clock_normalization_preserves_relative_chronology(self):
        decision_ts = 1_800_000_000_000
        ref = 1_900_000_000_000
        k15 = _candles(3, 15, decision_ts - 15 * 60 * 1000)
        k1h = _candles(3, 60, decision_ts - 60 * 60 * 1000)
        k4h = _candles(3, 240, decision_ts - 240 * 60 * 1000)
        n15, n1h, n4h = clock_normalized_windows(
            k15, k1h, k4h, decision_ts, reference_now_ms=ref
        )
        self.assertEqual(n15[-1]["ts"], ref - 15 * 60 * 1000)
        self.assertEqual(n1h[-1]["ts"], ref - 60 * 60 * 1000)
        self.assertEqual(n4h[-1]["ts"], ref - 240 * 60 * 1000)
        self.assertEqual(n15[1]["ts"] - n15[0]["ts"], 15 * 60 * 1000)
        self.assertEqual(k15[-1]["ts"], decision_ts - 15 * 60 * 1000)

    def test_net_backtest_return_is_normalized_to_initial_risk(self):
        trade = {"pnl_pct": -0.01, "entry_fill": 100.0}
        self.assertAlmostEqual(_candidate_r_multiple(trade, 100.0, 99.0), -1.0)
        trade = {"pnl_pct": 0.015, "entry_fill": 100.0}
        self.assertAlmostEqual(_candidate_r_multiple(trade, 100.0, 99.0), 1.5)

    def test_rejected_candidate_keeps_known_counterfactual_outcome(self):
        import bot.strategy as strategy

        decision_ts = 1_800_000_000_000
        k15 = _candles(80, 15, decision_ts - 15 * 60 * 1000)
        k1h = _candles(50, 60, decision_ts - 60 * 60 * 1000)
        k4h = _candles(25, 240, decision_ts - 240 * 60 * 1000)
        trade = {"ts": decision_ts, "pnl_pct": -0.01, "entry_fill": 100.0}

        captured = {}

        class FakeAnalyzer:
            def analyze_mtf(self, symbol, a15, a1h, a4h, **kwargs):
                captured["last15"] = a15[-1]["ts"]
                captured["lens"] = (len(a15), len(a1h), len(a4h))
                return SimpleNamespace(entry=100.0, sl=99.0, tp=102.0, rr=2.0)

        def reject(*args, **kwargs):
            return SimpleNamespace(execution_allowed=False, confidence=40.0)

        original = strategy.Analyzer
        strategy.Analyzer = FakeAnalyzer
        try:
            evidence = build_nexus_oos_candidates(
                "BTCUSDT", k15, k1h, k4h, [trade],
                decision_fn=reject,
                reference_now_ms=1_900_000_000_000,
            )
        finally:
            strategy.Analyzer = original

        self.assertEqual(evidence.parity, "CORE_CANDLES_ONLY")
        self.assertEqual(evidence.evaluated_count, 1)
        self.assertEqual(evidence.warmup_excluded_count, 0)
        self.assertEqual(captured["last15"], decision_ts - 15 * 60 * 1000)
        self.assertEqual(captured["lens"], (60, 20, 15))
        row = evidence.candidates[0]
        self.assertTrue(row.baseline_eligible)
        self.assertFalse(row.approved)
        self.assertTrue(row.outcome_known)
        self.assertAlmostEqual(row.r_multiple, -1.0)
        self.assertAlmostEqual(row.confidence, 0.4)

    def test_nexus_warmup_is_excluded_not_mislabeled_as_rejected(self):
        import bot.strategy as strategy

        decision_ts = 1_800_000_000_000
        k15 = _candles(80, 15, decision_ts - 15 * 60 * 1000)
        k1h = _candles(25, 60, decision_ts - 60 * 60 * 1000)
        k4h = _candles(18, 240, decision_ts - 240 * 60 * 1000)
        trade = {"ts": decision_ts, "pnl_pct": 0.02, "entry_fill": 100.0}

        class FakeAnalyzer:
            def analyze_mtf(self, *args, **kwargs):
                return SimpleNamespace(entry=100.0, sl=99.0, tp=102.0, rr=2.0)

        original = strategy.Analyzer
        strategy.Analyzer = FakeAnalyzer
        try:
            evidence = build_nexus_oos_candidates(
                "BTCUSDT", k15, k1h, k4h, [trade],
                decision_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not evaluate")),
            )
        finally:
            strategy.Analyzer = original

        self.assertEqual(evidence.evaluated_count, 0)
        self.assertEqual(evidence.warmup_excluded_count, 1)
        row = evidence.candidates[0]
        self.assertFalse(row.baseline_eligible)
        self.assertTrue(row.outcome_known)
        self.assertAlmostEqual(row.r_multiple, 2.0)


if __name__ == "__main__":
    unittest.main()
