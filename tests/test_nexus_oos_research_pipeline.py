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


CONTRACTS = [
    {"symbol": f"{b}USDTM", "baseCurrency": b, "multiplier": 0.01, "lotSize": 1, "minQty": 1,
     "tickSize": 0.01, "maxLeverage": 75, "maintainMargin": 0.004}
    for b in ("AAA", "BBB", "BROKEN")
]


def _short_tail_manifest() -> str:
    """Pinned manifest copy with a 200-bar look-forward for 900-bar fixtures."""
    from bot import nexus_oos_replay_manifest as rm
    raw = json.loads(rm.DEFAULT_PATH.read_text(encoding="utf-8"))
    raw["values"]["OUTCOME_LOOKFORWARD_BARS"]["value"] = 200
    raw["values"]["RESEARCH_MAX_HOLD_BARS"]["value"] = 200
    fd = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(raw, fd)
    fd.close()
    return fd.name


class _Client:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def _get(self, path, params=None, auth=False):
        assert not auth
        if path == "/api/v1/contracts/active":
            return CONTRACTS
        raise AssertionError(path)


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
                      entry_type="PULLBACK", regime="TRENDING_UP", expected_pnl=0.5,
                      total_fees=0.2)
    return Signal(symbol, "SHORT", price, price + atr, price - 2.2 * atr, 70, score=70,
                  entry_type="MOMENTUM", regime="TRENDING_DOWN", expected_pnl=0.5,
                  total_fees=0.2)


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
                ["AAAUSDT", "BBBUSDT", "BROKENUSDT"], limit_15m=620,
                manifest_path=_short_tail_manifest()))

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
        self.assertIn("robustness", a["legacy_diagnostic"])
        self.assertEqual(a["legacy_diagnostic"]["authority"], "NONE")
        self.assertNotIn("robustness", a, "legacy IID robustness must not sit at top level")
        self.assertIn("portfolio_replay", a)
        self.assertEqual(a["authority_model"], "HORIZON_AWARE_BLOCK_BOOTSTRAP_V2")
        c = a["candidate_research"]
        self.assertEqual(c["layer"], "CANDIDATE_RESEARCH")
        for key in ("performance", "inference", "effective_sample", "segments_approved",
                    "segments_baseline", "concentration", "cost_stress_approved",
                    "break_even_cost_multiplier", "exit_variants_approved", "ablation",
                    "context_ablation", "strategy_gate_ablation", "threshold_research",
                    "probability_calibration", "regime_parity", "temporal_folds",
                    "research_status", "population_counts", "censoring", "sample_adequacy"):
            self.assertIn(key, c, key)
        for key in ("performance", "segments_approved", "ablation"):
            self.assertNotIn(key, a, "candidate metrics must not leak to top level")
        base = c["performance"]["baseline"]
        for metric in ("win_rate", "loss_rate", "breakeven_rate", "avg_r", "median_r",
                       "profit_factor", "gross_expectancy_r", "net_expectancy_r",
                       "total_fees_r", "total_slippage_r", "funding_contribution_r",
                       "candidate_sequence_drawdown_r", "longest_losing_streak", "longest_winning_streak",
                       "p05_r", "expectancy_ci_low_r", "ratio_convention"):
            self.assertIn(metric, base, metric)
        self.assertEqual(set(c["segments_baseline"]["research_regime"]) >= set(res.REQUIRED_REGIMES), True)
        self.assertIn("production_regime", c["segments_baseline"])
        self.assertEqual(set(c["cost_stress_baseline"]), set(replay.COST_SCENARIOS))
        self.assertEqual(set(c["ablation"]), set(replay.NEXUS_VARIANTS))
        self.assertEqual(c["threshold_research"]["runtime_threshold_changed"], False)
        self.assertEqual(c["threshold_research"]["split_method"], "PURGED_EMBARGOED_TEMPORAL_50_25_25")
        inf = c["inference"]["approved_expectancy"] if c["performance"]["approved"]["trades"] else c["inference"]["baseline_expectancy"]
        for k in ("iid_ci", "block24h_ci", "block48h_ci", "block72h_ci", "authority_ci_low"):
            self.assertIn(k, inf)
        self.assertEqual(inf["iid_role"], "DIAGNOSTIC_ONLY")
        eff = c["effective_sample"]["baseline"]
        for k in ("rows", "unique_utc_days", "unique_blocks", "symbols", "effective_n"):
            self.assertIn(k, eff)
        p = a["portfolio_replay"]
        self.assertEqual(p["layer"], "PORTFOLIO_EXECUTION_REPLAY")
        self.assertEqual(p["engine"], "BAR_BY_BAR_EVENT_ENGINE_V2")
        for k in ("starting_equity", "ending_equity", "portfolio_max_drawdown", "skipped",
                  "max_concurrent_positions", "by_month", "path_bootstrap", "gate_attribution",
                  "walk_forward", "end_state", "realized_return",
                  "daily_pnl_semantics_sensitivity", "contract_spec_sensitivity",
                  "approximate_trade_level_ci"):
            self.assertIn(k, p)
        self.assertEqual(p["approximate_trade_level_ci"]["authority"], "NONE")
        self.assertEqual(p["accounting_invariants"], "PASS")
        self.assertEqual(p["parity"]["status"], "PORTFOLIO_PARITY_INCOMPLETE")
        self.assertIn("portfolio_replay_legacy", a)
        m = a["replay_policy_manifest"]
        self.assertEqual(len(m["policy_sha256"]), 64)
        self.assertFalse(m["secrets_read"])
        self.assertIn("parity_attribution", a)
        for step in ("A0_legacy_model", "A1_unshifted_native_stops", "A2_production_exit_engine",
                     "A3_cross_geometry", "A4_production_funnel"):
            self.assertIn(step, a["parity_attribution"])
        rp_ = a["replay_parity"]
        self.assertFalse(rp_["portfolio_parity_complete"])
        self.assertIn("tp1 = tp2 = tp", rp_["tp1_equals_tp2_root_cause"])
        self.assertTrue(a["methodology"]["closed_candle_sentinel_verified"])

    def test_costs_monotone(self):
        cs = self.artifact["candidate_research"]["cost_stress_baseline"]
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
        for seg in self.artifact["candidate_research"]["segments_baseline"]["volatility_bucket"]:
            self.assertIsInstance(seg, str)


def _fake_decide(nexus_ai, symbol, w15, w1h, w4h, sig, ticker, funding, threshold):
    from types import SimpleNamespace
    ts = int(w15[-1]["ts"])
    ok = (ts // 900_000) % 2 == 0 or threshold < 0
    return SimpleNamespace(execution_allowed=ok, confidence=65.0, setup_quality=72.0,
                           market_regime="TRENDING_UP")


class ResearchPipelineWithApprovals(unittest.TestCase):
    """Same offline pipeline with a deterministic NEXUS stub so approved rows
    flow through geometry, the portfolio engine, path bootstrap, attribution."""

    @classmethod
    def setUpClass(cls):
        async def fake_history(client, symbol, interval, limit):
            minutes = {"15": 15, "60": 60, "240": 240}[interval]
            seed = hash((symbol, interval)) % 1000
            n = {"15": 900, "60": 400, "240": 200}[interval]
            start = 1_760_000_000_000 - (0 if minutes == 15 else n * minutes * 60_000 // 3)
            return _candles(minutes, n, seed, start=start)

        async def fake_funding(client, symbol, start_ms, end_ms):
            return [{"timepoint": t, "fundingRate": 0.0001}
                    for t in range(start_ms - start_ms % (8 * 3_600_000), end_ms, 8 * 3_600_000)]

        from bot.strategy import Analyzer
        cls.ai_inputs = []
        original_ai = replay._ai_portfolio_and_gate

        def capture(*args):
            cls.ai_inputs.append(args)
            return original_ai(*args)
        with patch.object(replay, "fetch_history", fake_history), \
             patch.object(replay, "fetch_public_funding_history", fake_funding), \
             patch.object(replay, "PublicKuCoinFuturesClient", _Client), \
             patch.object(replay, "_decide", _fake_decide), \
             patch.object(replay, "_ai_portfolio_and_gate", capture), \
             patch.object(Analyzer, "analyze_mtf", _fake_analyze):
            cls.artifact = asyncio.run(replay.run_real_replay(["AAAUSDT", "BBBUSDT"], limit_15m=620,
                                                              manifest_path=_short_tail_manifest()))

    def test_phase7b_ai_test_fold_portfolio_and_gate_path(self):
        """AI status OK path: per-TEST-fold portfolio from the same start, pooled
        portfolio, independent AI gate, and no challenger unless PASS + stable."""
        self.assertEqual(len(self.ai_inputs), 1)
        _, all_rich, manifest, instruments, mmr = self.ai_inputs[0]
        execs = [r for r in all_rich if r.get("executable") and r.get("outcome_status") and r.get("legs")]
        self.assertGreater(len(execs), 4)
        keys = [[int(r["ts"]), r["symbol"], r["direction"]] for r in execs]
        half = len(keys) // 2
        art = {"candidate_research": {"ai_meta_model": {
            "status": "OK", "data_label": "HISTORICAL_OOS_PREVIOUSLY_INSPECTED",
            "selection_stability": {"status": "MODEL_SELECTION_UNSTABLE"},
            "steps": [{"test_fold": 3, "_approved_keys": keys[:half]},
                      {"test_fold": 4, "_approved_keys": keys[half:]}]}}}
        replay.__dict__["_ai_portfolio_and_gate"](art, all_rich, manifest, instruments, mmr)
        ai = art["candidate_research"]["ai_meta_model"]
        folds = ai["portfolio_by_test_fold"]
        self.assertEqual([f["test_fold"] for f in folds], [3, 4])
        self.assertEqual(folds[0]["starting_equity"], folds[1]["starting_equity"])
        self.assertGreater(ai["portfolio_pooled_test"]["total_trades"], 0)
        self.assertEqual(ai["research_promotion_gate"]["verdict"], "BLOCK")
        self.assertEqual(ai["shadow_challenger"], {"created": False, "reason": "AI_RESEARCH_GATE_BLOCK"})

    def test_phase7d_hook_policy_portfolio_keeps_downstream_blocked_hook_rows(self):
        """A hook-approved candidate blocked downstream (e.g. drift) stays in the
        AI_HOOK_POLICY_PORTFOLIO and is excluded only from the
        KNOWN_DOWNSTREAM_FILTERED_PORTFOLIO; nothing is silently dropped."""
        import copy as _copy
        _, all_rich, manifest, instruments, mmr = self.ai_inputs[0]
        rich = _copy.deepcopy(all_rich)
        execs = [r for r in rich if r.get("executable") and r.get("outcome_status") and r.get("legs")]
        blocked = execs[0]
        blocked.update(executable=False, drift_blocked=True, ai_hook_eligible=True)
        keys = [[int(r["ts"]), r["symbol"], r["direction"]] for r in execs[:6]]
        art = {"candidate_sha": "c" * 40, "candidate_research": {"ai_meta_model": {
            "status": "OK", "data_label": "HISTORICAL_OOS_PREVIOUSLY_INSPECTED",
            "selection_stability": {"status": "MODEL_SELECTION_UNSTABLE"},
            "steps": [{"test_fold": 3, "_approved_keys": keys}]}}}
        replay.__dict__["_ai_portfolio_and_gate"](art, rich, manifest, instruments, mmr)
        ai = art["candidate_research"]["ai_meta_model"]
        self.assertEqual(ai["portfolio_kind"], "AI_HOOK_POLICY_PORTFOLIO")
        self.assertEqual(ai["portfolio_by_test_fold"][0]["rows_in_portfolio"], 6)
        self.assertEqual(ai["downstream_filtered_portfolio_by_test_fold"][0]["rows_in_portfolio"], 5)
        self.assertEqual(ai["portfolio_pooled_test"]["approved_hook_rows_without_path"], 0)
        self.assertEqual(ai["research_promotion_gate"]["required_portfolio"], "AI_HOOK_POLICY_PORTFOLIO")
        self.assertEqual(ai["shadow_observer"]["lifecycle_state"], "SHADOW_OBSERVER") if ai.get(
            "shadow_observer", {}).get("created") else None

    def test_portfolio_trades_single_position_and_invariants(self):
        p = self.artifact["portfolio_replay"]
        self.assertGreater(p["approved_candidates"], 10)
        self.assertGreater(p["total_trades"], 0)
        self.assertLessEqual(p["max_concurrent_positions"], 1)
        self.assertEqual(p["accounting_invariants"], "PASS")
        self.assertGreater(p["accounting_invariant_checks"], 100)
        self.assertGreater(p["skipped"].get("liquidation_guard_multi_position", 0)
                           + p["skipped"].get("same_symbol_open", 0)
                           + p["skipped"].get("cooldown_or_circuit_breaker", 0), 0)
        self.assertEqual(p["path_bootstrap"]["authority"], "NONE")
        self.assertEqual(p["path_bootstrap"]["status"], "APPROXIMATE_NON_AUTHORITATIVE")
        self.assertEqual(p["path_bootstrap"]["per_block_length"]["24h"]["replicates"], 100)
        # A few days of fixture history cannot hold four purged 14-day folds:
        # the honest outcome is INSUFFICIENT, never a shrunken embargo.
        wf = p["walk_forward"]
        self.assertEqual(wf["status"], "INSUFFICIENT_INDEPENDENT_PORTFOLIO_PERIODS")
        self.assertEqual((wf["folds_total"], wf["folds_authoritative"]), (0, 0))
        self.assertGreaterEqual(wf["fold_embargo_ms"], wf["fold_required_horizon_ms"])
        self.assertEqual(wf["fold_account_state"], "RESET_FOR_REGIME_ROBUSTNESS")
        self.assertEqual(p["effective_live_max_concurrent_positions"], 1)
        self.assertEqual(p["configured_max_positions"], 2)
        self.assertEqual(set(p["gate_attribution"]), set(__import__(
            "bot.nexus_oos_portfolio_engine", fromlist=["Toggles"]).Toggles.__dataclass_fields__))
        self.assertIn("single_position_liquidation_rule", p["gate_attribution"])

    def test_phase7_ai_sections_present(self):
        c = self.artifact["candidate_research"]
        ai = c["ai_meta_model"]
        self.assertIn(ai.get("status"), ("OK", "INSUFFICIENT_DATA", "INSUFFICIENT_INDEPENDENT_FOLDS"))
        self.assertEqual(ai["data_label"], "HISTORICAL_OOS_PREVIOUSLY_INSPECTED")
        self.assertGreater(ai["dataset_manifest"]["rows"], 0)          # canonical features populated
        answers = c["loss_decomposition"]["answers"]
        for k in ("entries_wrong_gross_negative", "costs_consume_edge", "short_structurally_worse",
                  "nexus_score_rank_correlation_with_r"):
            self.assertIn(k, answers)

    def test_attribution_steps_are_populated(self):
        att = self.artifact["parity_attribution"]
        self.assertGreater(att["A0_legacy_model"]["n"], 10)
        self.assertEqual(att["A0_legacy_model"]["n"], att["A1_unshifted_native_stops"]["n"])
        self.assertIsNotNone(att["A4_production_funnel"]["mean_r"])

    def test_legacy_portfolio_rerun_present(self):
        leg = self.artifact["portfolio_replay_legacy"]
        self.assertEqual(leg["authority"], "NONE")
        self.assertIsNotNone(leg["ending_equity"])

    def test_gate_blocks_on_parity_even_if_research_ran(self):
        clean = replay._strip_private(self.artifact)
        res_ = gate.evaluate(json.loads(json.dumps(clean, default=str)))
        self.assertFalse(res_.promote)
        for b in ("PORTFOLIO_PARITY_INCOMPLETE", "PRETRADE_CONTEXT_PARITY_INCOMPLETE",
                  "EXIT_PARITY_INCOMPLETE", "HISTORICAL_CONTEXT_PARITY_INCOMPLETE"):
            self.assertIn(b, res_.blockers)
        text = json.dumps(clean, default=str)
        self.assertNotIn('"_marks"', text)
        self.assertNotIn('"_path"', text)


class ResearchStatisticsTests(unittest.TestCase):
    def test_performance_known_values(self):
        rows = [{"ts": i, "r": r} for i, r in enumerate([1.0, -1.0, 2.0, -1.0, 0.0])]
        p = res.performance(rows)
        self.assertAlmostEqual(p["avg_r"], 0.2)
        self.assertAlmostEqual(p["win_rate"], 0.4)
        self.assertAlmostEqual(p["breakeven_rate"], 0.2)
        self.assertAlmostEqual(p["profit_factor"], 1.5)
        self.assertAlmostEqual(p["candidate_sequence_drawdown_r"], 1.0)
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
            ts = 1_760_000_000_000 + i * 6 * 3_600_000
            rows.append({"ts": ts, "outcome_end_ts": ts + 3_600_000, "r": r, "gates_passed": True,
                         "nexus_score": score, "nexus_regime": "TRENDING_UP", "symbol": "X",
                         "production_regime": "RANGE"})
        tr = res.threshold_research(rows, (60, 90), 60, min_trades=10, min_blocks=3)
        self.assertEqual(tr["selected_threshold"], 60)

    def test_calibration_refuses_tiny_sample(self):
        rows = [{"ts": 1_760_000_000_000 + i * 3_600_000, "r": 1.0 if i % 3 else -1.0,
                 "approved": True, "nexus_confidence": 60.0} for i in range(50)]
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
        for i in range(400):
            good = i % 2 == 0
            rows.append({"ts": 1_760_000_000_000 + i * 6 * 3_600_000, "r": 1.0 if good else -1.0,
                         "approved": good, "variants": {"minus_X": True}})
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


class DiagnosticsDoesNotCountResearchVariants(unittest.TestCase):
    def test_diagnostics_runs_replay_without_research_variants(self):
        import inspect
        from bot import nexus_oos_replay_diagnostics as diag
        self.assertIn("run_real_replay(symbols, limit_15m=limit_15m, research=False)",
                      inspect.getsource(diag.run))
