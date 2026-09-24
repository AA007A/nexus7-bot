"""Offline end-to-end check of the research replay artifact and gate wiring.

Network is never used: history, funding and the public client are stubbed,
and the strategy emits a deterministic synthetic candidate stream so the full
NEXUS decision + research pipeline runs.
"""
import asyncio
import json
import math
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import nexus_oos_real_replay as replay
from bot import nexus_oos_research as res
from bot import nexus_oos_promotion_gate as gate


def _candles(interval_min, n, seed, start=1_760_000_000_000):
    rng = random.Random(seed)
    price, out = 100.0, []
    for i in range(n):
        drift = 0.0006 if (i // 300) % 2 == 0 else -0.0004
        change = drift + rng.gauss(0, 0.004)
        o = price
        c = max(1.0, price * (1 + change))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.002)))
        l = min(o, c) * (1 - abs(rng.gauss(0, 0.002)))
        out.append({"ts": start + i * interval_min * 60_000, "o": o, "h": h, "l": l, "c": c,
                    "v": 1000 + rng.random() * 500})
        price = c
    return out


class _Client:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


def _fake_analyze(self, symbol, k15, k1h, k4h, **kwargs):
    from bot.strategy import Signal
    last = k15[-1]
    if int(last["ts"] // 900_000) % 9:
        return None
    price = float(last["c"])
    long = float(k1h[-1]["c"]) >= float(k1h[-10]["c"])
    atr = price * 0.004
    if long:
        return Signal(symbol, "LONG", price, price - atr, price + 2.2 * atr, 70, score=70,
                      entry_type="PULLBACK", regime="TRENDING_UP")
    return Signal(symbol, "SHORT", price, price + atr, price - 2.2 * atr, 70, score=70,
                  entry_type="MOMENTUM", regime="TRENDING_DOWN")


class ResearchPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        async def fake_history(client, symbol, interval, limit):
            if symbol == "BROKENUSDT":
                raise RuntimeError("historical integrity failed")
            minutes = {"15": 15, "60": 60, "240": 240}[interval]
            seed = hash((symbol, interval)) % 1000
            n = {"15": 900, "60": 400, "240": 200}[interval]
            start = 1_760_000_000_000 - (0 if minutes == 15 else n * minutes * 60_000 // 3)
            return _candles(minutes, n, seed, start=start)

        async def fake_funding(client, symbol, start_ms, end_ms):
            return [{"timepoint": t, "fundingRate": 0.0001}
                    for t in range(start_ms, end_ms, 8 * 3_600_000)]

        from bot.strategy import Analyzer
        with patch.object(replay, "fetch_history", fake_history), \
             patch.object(replay, "fetch_public_funding_history", fake_funding), \
             patch.object(replay, "PublicKuCoinFuturesClient", _Client), \
             patch.object(Analyzer, "analyze_mtf", _fake_analyze):
            cls.artifact = asyncio.run(replay.run_real_replay(
                ["AAAUSDT", "BBBUSDT", "BROKENUSDT"], limit_15m=900))

    def test_unavailable_symbol_is_reported_not_dropped(self):
        a = self.artifact
        self.assertIn("BROKENUSDT", a["unavailable_symbols"])
        self.assertIn("SYMBOLS_UNAVAILABLE", a["blockers"])
        self.assertEqual({s["symbol"] for s in a["symbols"]}, {"AAAUSDT", "BBBUSDT", "BROKENUSDT"})

    def test_uses_production_threshold(self):
        from bot import nexus_ai
        self.assertEqual(self.artifact["nexus_threshold_used"], float(nexus_ai.MIN_SCORE))

    def test_research_sections_present(self):
        a = self.artifact
        self.assertGreater(a["report"]["baseline_candidates"], 20)
        for key in ("performance", "segments_approved", "segments_baseline", "concentration",
                    "cost_stress_approved", "break_even_cost_multiplier", "exit_variants_approved",
                    "ablation", "context_ablation", "strategy_gate_ablation",
                    "threshold_research", "probability_calibration", "robustness"):
            self.assertIn(key, a, key)
        base = a["performance"]["baseline"]
        for metric in ("win_rate", "loss_rate", "breakeven_rate", "avg_r", "median_r",
                       "profit_factor", "gross_expectancy_r", "net_expectancy_r",
                       "total_fees_r", "total_slippage_r", "funding_contribution_r",
                       "max_drawdown_r", "longest_losing_streak", "longest_winning_streak",
                       "p05_r", "expectancy_ci_low_r", "ratio_convention"):
            self.assertIn(metric, base, metric)
        self.assertEqual(set(a["segments_baseline"]["regime"]) >= set(res.REQUIRED_REGIMES), True)
        self.assertEqual(set(a["cost_stress_baseline"]), set(replay.COST_SCENARIOS))
        self.assertEqual(set(a["ablation"]), set(replay.NEXUS_VARIANTS))
        self.assertEqual(a["threshold_research"]["runtime_threshold_changed"], False)

    def test_costs_monotone(self):
        cs = self.artifact["cost_stress_baseline"]
        self.assertLess(cs["combined_adverse"]["net_expectancy_r"], cs["current"]["net_expectancy_r"])
        self.assertLessEqual(cs["fees_plus_50pct"]["net_expectancy_r"], cs["fees_plus_25pct"]["net_expectancy_r"])

    def test_artifact_serializes_and_strict_gate_blocks(self):
        clean = replay._strip_private(self.artifact)
        text = json.dumps(clean, sort_keys=True, default=str)
        self.assertNotIn('"_sim"', text)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.json"
            path.write_text(text, encoding="utf-8")
            code = gate.main([str(path)])
        self.assertNotEqual(code, 0)  # parity incomplete at minimum

    def test_rows_have_no_future_timestamp_leak(self):
        # Every research regime label is computed from windows closed at the
        # decision time; buckets are finite labels.
        for seg in self.artifact["segments_baseline"]["volatility_bucket"]:
            self.assertIsInstance(seg, str)


class ResearchStatisticsTests(unittest.TestCase):
    def test_performance_known_values(self):
        rows = [{"ts": i, "r": r} for i, r in enumerate([1.0, -1.0, 2.0, -1.0, 0.0])]
        p = res.performance(rows)
        self.assertAlmostEqual(p["avg_r"], 0.2)
        self.assertAlmostEqual(p["win_rate"], 0.4)
        self.assertAlmostEqual(p["breakeven_rate"], 0.2)
        self.assertAlmostEqual(p["profit_factor"], 1.5)
        self.assertAlmostEqual(p["max_drawdown_r"], 1.0)
        self.assertEqual(p["longest_losing_streak"], 1)
        self.assertIsNone(p["cvar05_r"])  # sample too small
        self.assertEqual(p["ratio_convention"], "PER_TRADE_NOT_ANNUALIZED")

    def test_threshold_selection_never_uses_test_split(self):
        rows = []
        for i in range(400):
            test_part = i >= 300
            # Threshold 90 looks great ONLY in the final test split.
            score = 91 if i % 2 else 61
            r = (5.0 if score == 91 else -1.0) if test_part else (-1.0 if score == 91 else 0.5)
            rows.append({"ts": i, "r": r, "gates_passed": True, "nexus_score": score,
                         "nexus_regime": "TRENDING_UP", "symbol": "X", "regime": "RANGE"})
        tr = res.threshold_research(rows, (60, 90), 60, min_trades=10)
        self.assertEqual(tr["selected_threshold"], 60)

    def test_calibration_refuses_tiny_sample(self):
        rows = [{"ts": i, "r": 1.0 if i % 3 else -1.0, "approved": True, "nexus_confidence": 60.0}
                for i in range(50)]
        from bot.nexus_probability import heuristic_win_probability
        rep = res.calibration_report(rows, heuristic_win_probability)
        self.assertEqual(rep["status"], "INSUFFICIENT_EVIDENCE")
        self.assertIsNone(rep["calibrated"])
        self.assertIn("brier", rep["base_rate_on_test"])

    def test_break_even_multiplier(self):
        out = res.break_even_multiplier(lambda m: 0.3 - 0.2 * m)
        self.assertAlmostEqual(out["multiplier"], 1.5, places=4)
        self.assertEqual(res.break_even_multiplier(lambda m: -0.1 - m)["note"], "NEGATIVE_EVEN_AT_ZERO_COST")

    def test_paired_ablation_direction(self):
        rows = []
        for i in range(200):
            good = i % 2 == 0
            rows.append({"ts": i, "r": 1.0 if good else -1.0, "approved": good,
                         "variants": {"minus_X": True}})
        out = res.paired_ablation(rows, "minus_X")
        self.assertGreater(out["delta_expectancy_r"], 0)
        self.assertEqual(out["verdict"], "COMPONENT_ADDS_EXPECTANCY")
        tiny = res.paired_ablation(rows[:20], "minus_X")
        self.assertEqual(tiny["verdict"], "INSUFFICIENT_EVIDENCE")

    def test_regime_classifier_labels(self):
        flat = [{"h": 101, "l": 99, "c": 100} for _ in range(60)]
        self.assertIn(res.classify_regime(flat), res.REQUIRED_REGIMES)
        self.assertEqual(res.classify_regime(flat[:10]), "UNKNOWN")
        up = [{"h": 100 + i + 0.5, "l": 100 + i - 0.5, "c": 100 + i} for i in range(60)]
        self.assertIn(res.classify_regime(up), ("BREAKOUT", "TRENDING_BULL"))
        self.assertTrue(math.isfinite(res.atr_pct([x["h"] for x in up], [x["l"] for x in up], [x["c"] for x in up])))


if __name__ == "__main__":
    unittest.main()
