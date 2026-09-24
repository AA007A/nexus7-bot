"""Dependence-aware inference, purged splits and portfolio-replay invariants."""
import random
import unittest
from collections import Counter

from bot import nexus_oos_inference as inf
from bot import nexus_oos_portfolio_replay as pr
from bot import nexus_oos_research as res
from bot import risk_policy as rp

T0 = 1_760_000_000_000
H = inf.HOUR_MS


def _policy(**over):
    base = dict(leverage=50.0, max_risk_pct=0.01, max_margin_pct=0.5, max_drawdown=0.5,
                max_positions=2, daily_stop_loss_pct=0.03, daily_stop_loss_abs=0.0,
                min_rr_ratio=2.0)
    base.update(over)
    return rp.RiskPolicy(**base)


def _trade(ts, symbol="BTCUSDT", *, hours=2.0, r=1.0, direction="LONG", path=None, approved=True):
    entry = 100.0
    stop = 99.0 if direction == "LONG" else 101.0
    end = ts + int(hours * H)
    return {"ts": ts, "outcome_end_ts": end, "symbol": symbol, "direction": direction,
            "entry_fill": entry, "stop": stop, "risk_fraction": 0.01, "r": r,
            "approved": approved, "fees_r": -0.12, "slippage_r": -0.05, "funding_r": 0.0,
            "_path": path or [(end, entry * (1 + 0.01 * r) if direction == "LONG" else entry * (1 - 0.01 * r))],
            "month": "2025-10"}


class BlockBootstrapTests(unittest.TestCase):
    def test_all_same_block_cross_symbol_rows_are_drawn_together(self):
        rows = []
        for day in range(20):
            for sym in ("BTC", "ETH", "SOL"):
                for k in range(3):
                    rows.append({"ts": T0 + day * inf.DAY_MS + k * H, "r": random.random(),
                                 "symbol": sym, "id": (day, sym, k)})
        by_block = Counter(inf.block_id(r["ts"]) for r in rows)

        def stat(draw):
            counts = Counter(r["id"] for r in draw)
            per_block = {}
            for (day, sym, k), c in counts.items():
                per_block.setdefault(day, set()).add(c)
            # every row of a drawn block appears the same number of times
            for day, cs in per_block.items():
                self.assertEqual(len(cs), 1)
            blocks_drawn = Counter(inf.block_id(r["ts"]) for r in draw)
            for b, c in blocks_drawn.items():
                self.assertEqual(c % by_block[b], 0)
            return sum(r["r"] for r in draw) / len(draw)

        lo, hi = inf.block_bootstrap_ci(rows, stat, samples=150)
        self.assertIsNotNone(lo)

    def test_block_ci_wider_than_iid_under_block_dependence(self):
        rng = random.Random(3)
        rows = []
        for day in range(60):
            shock = rng.gauss(0, 1.0)  # common market-wide move for the whole day
            for k in range(20):
                rows.append({"ts": T0 + day * inf.DAY_MS + k * 600_000, "r": shock + rng.gauss(0, 0.2)})
        d = inf.dependence_aware_mean(rows, samples=600)
        iid_w = d["iid_ci"][1] - d["iid_ci"][0]
        blk_w = d["block24h_ci"][1] - d["block24h_ci"][0]
        self.assertGreater(blk_w, 2 * iid_w)
        self.assertLessEqual(d["authority_ci_low"], d["block24h_ci"][0])
        eff = inf.effective_sample(rows)
        self.assertLess(eff["effective_n"], len(rows) / 5)
        self.assertEqual(eff["unique_blocks"], 60)

    def test_iid_alone_never_carries_authority(self):
        rows = [{"ts": T0 + i, "r": 1.0 + i * 1e-6} for i in range(50)]  # one block only
        d = inf.dependence_aware_mean(rows, samples=200)
        self.assertIsNone(d["authority_ci_low"])
        self.assertIsNotNone(d["iid_ci"][0])


class IidZeroAuthorityInInference(unittest.TestCase):
    """P0-STAT-01: authority = min lower / max upper over VALID BLOCK intervals only."""

    def _rows(self):
        rng = random.Random(9)
        rows = []
        for day in range(90):
            shock = rng.gauss(0.05, 0.6)
            for k in range(12):
                rows.append({"ts": T0 + day * inf.DAY_MS + k * H, "r": shock + rng.gauss(0, 0.2),
                             "approved": k % 3 == 0})
        return rows

    def _block_only(self, d):
        cis = [d[k] for k in ("block24h_ci", "block48h_ci", "block72h_ci") if d[k][0] is not None]
        return min(c[0] for c in cis), max(c[1] for c in cis)

    def test_mean_authority_equals_block_only_interval(self):
        d = inf.dependence_aware_mean(self._rows(), samples=400)
        self.assertEqual((d["authority_ci_low"], d["authority_ci_high"]), self._block_only(d))
        self.assertEqual(d["iid_role"], "DIAGNOSTIC_ONLY")
        self.assertEqual(d["authority_model"], "BLOCK_BOOTSTRAP_ONLY_V1")
        self.assertIn("block72h_ci", d)

    def test_diff_authority_equals_block_only_interval(self):
        rows = self._rows()
        d = inf.dependence_aware_diff(rows, lambda r: r["approved"], lambda r: True, samples=400)
        self.assertEqual((d["authority_ci_low"], d["authority_ci_high"]), self._block_only(d))

    def test_iid_cannot_widen_or_narrow_authority(self):
        rows = self._rows()
        from unittest.mock import patch
        real = inf.fast_ci

        def fake(rows_, sel_a, sel_b=None, *, block_ms, samples=inf.BOOTSTRAP_SAMPLES, seed=inf.SEED):
            if block_ms is None:
                return (-99.0, 99.0)   # absurd IID interval
            return real(rows_, sel_a, sel_b, block_ms=block_ms, samples=samples, seed=seed)

        base = inf.dependence_aware_mean(rows, samples=300)
        with patch.object(inf, "fast_ci", fake):
            wild = inf.dependence_aware_mean(rows, samples=300)
        self.assertEqual(wild["iid_ci"], [-99.0, 99.0])
        self.assertEqual((wild["authority_ci_low"], wild["authority_ci_high"]),
                         (base["authority_ci_low"], base["authority_ci_high"]))

    def test_dependence_diagnostics_reports_lags_and_predeclared_blocks(self):
        dd = inf.dependence_diagnostics(self._rows())
        self.assertEqual(dd["block_lengths_predeclared_hours"], [24, 48, 72])
        self.assertEqual(set(dd["daily_acf"]), {"1", "2", "3"})
        self.assertEqual(set(dd["per_block_length_lag1"]), {"24h", "48h", "72h"})


class PurgedSplitTests(unittest.TestCase):
    def _rows(self):
        rows = []
        for i in range(600):
            ts = T0 + i * H
            rows.append({"ts": ts, "outcome_end_ts": ts + 9 * H, "r": 0.1})
        return rows

    def test_purge_and_embargo(self):
        rows = self._rows()
        sp = inf.purged_split(rows)
        self.assertGreaterEqual(sp["embargo_ms"], inf.REPLAY_MAX_HORIZON_MS)
        self.assertGreaterEqual(sp["embargo_ms"], 9 * H)
        inf.assert_no_leakage(sp)
        b1, b2 = sp["boundaries_ms"]
        self.assertTrue(all(r["outcome_end_ts"] < b1 for r in sp["train"]))
        self.assertTrue(all(r["ts"] >= b1 + sp["embargo_ms"] for r in sp["validation"]))
        self.assertTrue(all(r["outcome_end_ts"] < b2 for r in sp["validation"]))
        self.assertTrue(all(r["ts"] >= b2 + sp["embargo_ms"] for r in sp["test"]))
        self.assertGreater(sp["purged"]["train"], 0)

    def test_embargo_grows_with_longest_observed_horizon(self):
        rows = self._rows()
        rows[10]["outcome_end_ts"] = rows[10]["ts"] + 30 * H
        self.assertGreaterEqual(inf.purged_split(rows)["embargo_ms"], 30 * H)

    def test_leakage_assertion_detects_overlap(self):
        sp = {"train": [{"ts": 0, "outcome_end_ts": 100}], "validation": [{"ts": 50, "outcome_end_ts": 60}],
              "test": []}
        with self.assertRaises(AssertionError):
            inf.assert_no_leakage(sp)


class ThresholdFinalTestIsolation(unittest.TestCase):
    def _rows(self, test_r):
        rng = random.Random(5)
        rows = []
        for i in range(1200):
            ts = T0 + i * 2 * H
            score = rng.choice((58, 63, 68, 72, 77, 82, 88, 92))
            rows.append({"ts": ts, "outcome_end_ts": ts + 3 * H, "gates_passed": True,
                         "nexus_score": score, "nexus_regime": "TRENDING_UP", "symbol": "X",
                         "production_regime": "TRENDING_UP",
                         "r": (0.3 if score >= 70 else -0.3) + rng.gauss(0, 0.5)})
        cut = rows[0]["ts"] + int((rows[-1]["ts"] - rows[0]["ts"]) * 0.75)
        for r in rows:
            if r["ts"] >= cut:
                r["r"] = test_r(r)
        return rows

    def test_final_test_does_not_influence_selection(self):
        a = res.threshold_research(self._rows(lambda r: 5.0), (60, 65, 70, 75, 80), 60, min_trades=10)
        b = res.threshold_research(self._rows(lambda r: -5.0), (60, 65, 70, 75, 80), 60, min_trades=10)
        self.assertEqual(a["selected_threshold"], b["selected_threshold"])
        self.assertEqual([e["train"] for e in a["table"]], [e["train"] for e in b["table"]])
        self.assertEqual([e["validation"] for e in a["table"]], [e["validation"] for e in b["table"]])
        self.assertNotIn("test", a["table"][0])
        if a["validation_confirmed"]:
            self.assertEqual(a["status"], "CONFIRMED_ON_FINAL_TEST")
            self.assertEqual(b["status"], "EXPERIMENT_FAILED_ON_FINAL_TEST")
        self.assertFalse(a["runtime_threshold_changed"])


class CandidateVsPortfolioMetrics(unittest.TestCase):
    def test_metric_names_cannot_be_confused(self):
        perf = res.performance([{"ts": i, "r": r} for i, r in enumerate([1, -1, 2])])
        self.assertIn("candidate_sequence_drawdown_r", perf)
        self.assertNotIn("max_drawdown_r", perf)
        self.assertNotIn("portfolio_max_drawdown", perf)
        port = pr.run_portfolio_legacy([_trade(T0)], _policy())
        self.assertEqual(port["layer"], "PORTFOLIO_EXECUTION_REPLAY_LEGACY")
        self.assertIn("portfolio_max_drawdown", port)
        self.assertNotIn("candidate_sequence_drawdown_r", port)


class PortfolioReplayInvariants(unittest.TestCase):
    def test_max_positions_never_exceeded(self):
        rows = [_trade(T0 + i * 60_000, f"S{i}USDT", hours=5) for i in range(6)]
        out = pr.run_portfolio_legacy(rows, _policy(max_positions=2))
        self.assertEqual(out["max_concurrent_positions"], 2)
        self.assertEqual(out["total_trades"], 2)
        self.assertEqual(out["skipped"]["position_limit"], 4)

    def test_same_symbol_overlap_prevented(self):
        rows = [_trade(T0, "BTCUSDT", hours=5), _trade(T0 + H, "BTCUSDT", hours=5),
                _trade(T0 + 6 * H, "BTCUSDT", hours=1)]
        out = pr.run_portfolio_legacy(rows, _policy())
        self.assertEqual(out["skipped"]["same_symbol_open"], 1)
        self.assertEqual(out["total_trades"], 2)

    def test_capital_released_only_after_exit(self):
        rows = [_trade(T0, "AUSDT", hours=5), _trade(T0 + H, "BUSDT", hours=5),
                _trade(T0 + 2 * H, "CUSDT", hours=1),       # both slots busy -> skipped
                _trade(T0 + 5 * H + 1, "DUSDT", hours=1)]   # after A exits -> admitted
        out = pr.run_portfolio_legacy(rows, _policy(max_positions=2))
        self.assertEqual(out["total_trades"], 3)
        self.assertEqual(out["skipped"]["position_limit"], 1)

    def test_daily_stop_blocks_later_same_day_entries(self):
        losers = [_trade(T0 + i * H, f"L{i}USDT", hours=0.5, r=-1.0) for i in range(5)]
        later = [_trade(T0 + 10 * H, "XUSDT", hours=1)]
        out = pr.run_portfolio_legacy(losers + later, _policy(max_risk_pct=0.01, daily_stop_loss_pct=0.02))
        self.assertGreater(out["blocked_daily_stop"], 0)
        next_day = pr.run_portfolio_legacy(losers + [_trade(T0 + inf.DAY_MS + H, "XUSDT", hours=1)],
                                    _policy(max_risk_pct=0.01, daily_stop_loss_pct=0.02))
        self.assertEqual(next_day["blocked_daily_stop"], out["blocked_daily_stop"] - 1)

    def test_drawdown_hard_gate_blocks_subsequent_exposure(self):
        losers = [_trade(T0 + d * inf.DAY_MS, f"L{d}USDT", hours=1, r=-1.0) for d in range(12)]
        later = [_trade(T0 + 30 * inf.DAY_MS + d * inf.DAY_MS, f"W{d}USDT", hours=1, r=2.0) for d in range(5)]
        pol = _policy(max_risk_pct=0.02, max_drawdown=0.10, daily_stop_loss_pct=0.05)
        out = pr.run_portfolio_legacy(losers + later, pol)
        self.assertGreater(out["blocked_drawdown"], 0)
        self.assertLessEqual(out["ending_equity"], out["starting_equity"])

    def test_loss_per_trade_bounded_by_risk_budget(self):
        rows = [_trade(T0 + d * inf.DAY_MS, "AUSDT", hours=1, r=-1.0) for d in range(3)]
        out = pr.run_portfolio_legacy(rows, _policy(max_risk_pct=0.01))
        # r=-1 includes costs; sizing prices those costs, so each loss <= 1% of equity.
        self.assertGreater(out["ending_equity"], 1000.0 * (0.99 ** 3) - 1e-6)

    def test_deterministic_same_timestamp_ordering(self):
        rows = [_trade(T0, "ZUSDT"), _trade(T0, "AUSDT"), _trade(T0, "MUSDT")]
        out = pr.run_portfolio_legacy(rows, _policy(max_positions=2))
        self.assertEqual(set(out["by_symbol"]), {"AUSDT", "MUSDT"})
        self.assertIn("not profit-optimized", out["same_timestamp_ordering"])

    def test_contract_rules_from_public_metadata(self):
        rules, mmr = pr.contract_rules_from_public(
            [{"symbol": "XBTUSDTM", "baseCurrency": "XBT", "multiplier": 0.001, "lotSize": 1,
              "maintainMargin": 0.004},
             {"symbol": "ETHUSDTM", "baseCurrency": "ETH", "multiplier": 0.01, "lotSize": 1}],
            ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(set(rules), {"BTCUSDT", "ETHUSDT"})
        self.assertAlmostEqual(mmr["BTCUSDT"], 0.004)


if __name__ == "__main__":
    unittest.main()
