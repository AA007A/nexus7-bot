"""The PR promotion gate must exit non-zero on any unmet condition.

It also proves P0-STAT-01/02: IID intervals and legacy IID edge-gate outputs
have ZERO promotion authority in either direction.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from bot import nexus_oos_inference as inf
from bot import nexus_oos_promotion_gate as gate


DAY = 86_400_000
D0 = 1_735_689_600_000   # 2025-01-01T00:00Z


def residual(means, block_ids=None, counts=None):
    """Residual-dependence report exactly as inference emits it (from a series)."""
    ids = list(range(100, 100 + len(means))) if block_ids is None else list(block_ids)
    series = {"block_ids": ids, "means": list(means),
              "counts": [3] * len(means) if counts is None else list(counts)}
    return {"n_blocks": len(means), "series": series,
            **inf.acf_from_block_series(ids, list(means))}


def clean_series(n):
    import random
    for seed in range(1000):
        rng = random.Random(seed)
        m = [round(rng.gauss(0.1, 0.3), 12) for _ in range(n)]
        if not residual(m)["significant"]:
            return m
    raise AssertionError("no clean series")


def _folds(*, positive_key, required_ms=DAY, span_days=180):
    """Purged/embargoed calendar folds exactly as the replay lays them out."""
    lay = inf.purged_calendar_folds(D0, D0 + span_days * DAY, required_horizon_ms=required_ms)
    folds = [{**w, "max_market_ts_used": w["decision_end_ts"] + required_ms // 2}
             for w in lay["windows"]]
    return {"status": lay["status"], "folds": folds, "folds_total": len(folds),
            "folds_authoritative": len(folds), "folds_overlap_free": True,
            "fold_embargo_ms": lay["fold_embargo_ms"],
            "fold_required_horizon_ms": lay["fold_required_horizon_ms"], positive_key: len(folds)}


def _dep(lo24, hi24, lo48, hi48, lo72, hi72, *, iid=(0.01, 0.5), blocks=(90, 60, 45)):
    """Block intervals for 1/2/3-day blocks; horizon in the fixture is < 1 day,
    so all three are long enough; ``blocks`` = independent blocks per length."""
    ivs = [{"block_ms": d * DAY, "block_days": d, "ci": [lo, hi], "resampling_blocks": n,
            "authoritative": n >= 30, "residual_dependence": residual(clean_series(n))}
           for d, (lo, hi), n in zip((1, 2, 3), ((lo24, hi24), (lo48, hi48), (lo72, hi72)), blocks)]
    valid = [iv["ci"] for iv in ivs if iv["authoritative"]]
    lo = min(c[0] for c in valid) if valid else None
    hi = max(c[1] for c in valid) if valid else None
    return {"iid_ci": list(iid), "iid_role": "DIAGNOSTIC_ONLY", "block_intervals": ivs,
            "block24h_ci": [lo24, hi24], "block48h_ci": [lo48, hi48], "block72h_ci": [lo72, hi72],
            "required_block_ms": DAY, "authority_ci_low": lo, "authority_ci_high": hi}


def _passing_artifact():
    return {
        "authority_model": "HORIZON_AWARE_BLOCK_BOOTSTRAP_V2",
        "status": "AI_EDGE_PROVEN",
        "policy_parity": "POLICY_CONTENT_MATCH",
        "live_policy_observation": {"replay_policy_sha": "e" * 64},
        "candidate_sha": "c" * 40,
        "blockers": [],
        "historical_context_parity_complete": True,
        "replay_parity": {"exit_parity_complete": True, "pretrade_parity_complete": True,
                          "portfolio_parity_complete": True},
        "replay_policy_manifest": {"policy_sha256": "a" * 64},
        # Legacy IID report kept for compatibility; the gate must not read it.
        "report": {"known_baseline_outcomes": 2000, "known_approved_outcomes": 400,
                   "bootstrap_ci_low_r": 0.05, "nexus_expectancy_r": 0.20},
        "symbols": [
            {"symbol": s, "error": None, "history_days": 180.0,
             "historical_context": {"parity_complete": True}}
            for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT")
        ],
        "candidate_research": {
            "performance": {"baseline": {"trades": 2000},
                            "approved": {"trades": 400, "net_expectancy_r": 0.20}},
            "temporal_folds": _folds(positive_key="folds_positive_approved_expectancy"),
            "inference": {
                "approved_expectancy": _dep(0.05, 0.3, 0.04, 0.32, 0.045, 0.31),
                "uplift_vs_baseline": _dep(0.03, 0.2, 0.035, 0.21, 0.04, 0.19),
                "outcome_horizon_resolved_executable": {"max_ms": 20 * 3_600_000},
                "required_block_ms": DAY,
            },
            "censoring": {"censoring_material": False, "censored_count": 3, "censored_rate": 0.01},
            "sample_adequacy": {"resampling_blocks_approved": 90, "adequate": True},
            "effective_sample": {"approved": {"unique_blocks": 80, "effective_n": 250.0}},
            "concentration": {
                "approved_by_symbol": {"groups_positive": 5, "top_share_of_positive_r": 0.3},
                "approved_by_month": {"groups_positive": 3, "top_share_of_positive_r": 0.4},
            },
            "cost_stress_approved": {
                "fees_plus_50pct": {"net_expectancy_r": 0.1},
                "slippage_x2": {"net_expectancy_r": 0.08},
            },
        },
        "portfolio_replay": {
            "accounting_invariants": "PASS",
            "net_expectancy_r": 0.15, "starting_equity": 1000.0, "ending_equity": 1120.0,
            "portfolio_max_drawdown": 0.08, "research_max_drawdown_limit": 0.25,
            "realized_return": 0.10,
            "end_state": {"portfolio_censoring_material": False, "open_positions_at_end": 0},
            "walk_forward": {**_folds(positive_key="folds_positive"),
                             "fold_account_state": "RESET_FOR_REGIME_ROBUSTNESS"},
            "path_bootstrap": {"status": "APPROXIMATE_NON_AUTHORITATIVE", "authority": "NONE",
                               "net_return_ci": [0.02, 0.3]},
            "approximate_trade_level_ci": {"authority_ci_low": -0.5, "authority": "NONE"},
            "total_trades": 120, "contributing_symbols": 5,
        },
        "methodology": {"closed_candles_only": True, "closed_candle_sentinel_verified": True,
                        "historical_clock_frozen": True, "fees_included": True,
                        "slippage_included": True},
    }


# Final-head exact-SHA evidence before this phase (70b00b4, run 35957755830).
LEGACY_70B00B4 = {
    "status": "AI_EDGE_NOT_PROVEN",
    "blockers": ["HISTORICAL_CONTEXT_PARITY_INCOMPLETE", "UPLIFT_NOT_STATISTICALLY_POSITIVE"],
    "report": {"known_baseline_outcomes": 17597, "known_approved_outcomes": 4076,
               "bootstrap_ci_low_r": 0.011, "nexus_expectancy_r": -0.3236},
    "symbols": [{"symbol": "BTCUSDT", "historical_context": {"parity_complete": False}}],
    "candidate_research": {
        "performance": {"baseline": {"trades": 17597},
                        "approved": {"trades": 4076, "net_expectancy_r": -0.3236}},
        "inference": {
            "approved_expectancy": {"iid_ci": [-0.371, -0.273], "block24h_ci": [-0.459, -0.195],
                                    "block48h_ci": [-0.495, -0.179],
                                    "authority_ci_low": -0.495, "authority_ci_high": -0.179},
            "uplift_vs_baseline": {"iid_ci": [0.011, 0.093], "block24h_ci": [-0.034, 0.129],
                                   "block48h_ci": [-0.049, 0.132],
                                   "authority_ci_low": -0.049, "authority_ci_high": 0.132},
        },
    },
    "methodology": {"closed_candles_only": True, "historical_clock_frozen": True,
                    "fees_included": True, "slippage_included": True},
}


class PromotionGateTests(unittest.TestCase):
    def _run(self, data=None, *, raw=None, missing=False, extra=()):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.json"
            if not missing:
                path.write_text(raw if raw is not None else json.dumps(data), encoding="utf-8")
            return gate.main([str(path), *extra])

    def test_fully_proven_artifact_exits_zero(self):
        self.assertEqual(gate.evaluate(_passing_artifact()).blockers, [])
        self.assertEqual(self._run(_passing_artifact()), gate.EXIT_PROMOTE)

    def test_ai_edge_not_proven_exits_non_zero(self):
        a = _passing_artifact()
        a["status"] = "AI_EDGE_NOT_PROVEN"
        self.assertNotEqual(self._run(a), 0)

    def test_missing_artifact_exits_non_zero(self):
        self.assertEqual(self._run(missing=True), gate.EXIT_MISSING)

    def test_corrupt_artifact_exits_non_zero(self):
        self.assertEqual(self._run(raw="{not json"), gate.EXIT_CORRUPT)
        self.assertEqual(self._run(raw="[]"), gate.EXIT_CORRUPT)
        self.assertEqual(self._run({"status": "AI_EDGE_PROVEN"}), gate.EXIT_CORRUPT)

    def test_unsupported_authority_model_blocks(self):
        for model in (None, "IID", "LEGACY", "BLOCK_BOOTSTRAP_ONLY_V1"):
            a = _passing_artifact()
            a["authority_model"] = model
            self.assertIn("AUTHORITY_MODEL_UNSUPPORTED", gate.evaluate(a).blockers)
            self.assertNotEqual(self._run(a), 0)

    def test_incomplete_context_parity_blocks_production_promotion(self):
        a = _passing_artifact()
        a["historical_context_parity_complete"] = False
        self.assertNotEqual(self._run(a), 0)
        self.assertIn("HISTORICAL_CONTEXT_PARITY_INCOMPLETE", gate.evaluate(a).blockers)
        # Research-only flag relaxes exactly that condition.
        self.assertEqual(self._run(a, extra=("--allow-incomplete-context-parity",)), 0)

    def test_replay_parity_and_manifest_required(self):
        cases = {
            "EXIT_PARITY_INCOMPLETE": lambda a: a["replay_parity"].update(exit_parity_complete=False),
            "PRETRADE_CONTEXT_PARITY_INCOMPLETE": lambda a: a["replay_parity"].update(pretrade_parity_complete=False),
            "PORTFOLIO_PARITY_INCOMPLETE": lambda a: a["replay_parity"].update(portfolio_parity_complete=False),
            "REPLAY_PARITY_MISSING": lambda a: a.pop("replay_parity"),
            "REPLAY_POLICY_MANIFEST_MISSING": lambda a: a.pop("replay_policy_manifest"),
            "PORTFOLIO_ACCOUNTING_INVARIANTS_NOT_PROVEN": lambda a: a["portfolio_replay"].pop("accounting_invariants"),
            "METHODOLOGY_CLOSED_CANDLE_SENTINEL_VERIFIED_NOT_CONFIRMED":
                lambda a: a["methodology"].update(closed_candle_sentinel_verified=False),
        }
        for blocker, mutate in cases.items():
            a = copy.deepcopy(_passing_artifact())
            mutate(a)
            with self.subTest(blocker=blocker):
                r = gate.evaluate(a)
                self.assertIn(blocker, r.blockers)
                self.assertFalse(r.promote)
        # Research flag never relaxes replay parity.
        a = _passing_artifact()
        a["replay_parity"]["portfolio_parity_complete"] = False
        self.assertNotEqual(self._run(a, extra=("--allow-incomplete-context-parity",)), 0)

    def test_negative_approved_expectancy_blocks_even_with_positive_uplift(self):
        a = _passing_artifact()
        a["candidate_research"]["performance"]["approved"]["net_expectancy_r"] = -0.10
        r = gate.evaluate(a)
        self.assertIn("APPROVED_EXPECTANCY_NOT_POSITIVE", r.blockers)
        self.assertNotEqual(self._run(a), 0)

    def test_approved_expectancy_block_ci_must_be_positive(self):
        a = _passing_artifact()
        a["candidate_research"]["inference"]["approved_expectancy"] = _dep(0.05, 0.3, -0.01, 0.3, 0.04, 0.3)
        self.assertIn("APPROVED_EXPECTANCY_BLOCK_CI_NOT_POSITIVE", gate.evaluate(a).blockers)
        self.assertNotEqual(self._run(a), 0)

    def test_insufficient_sample_blocks(self):
        a = _passing_artifact()
        a["candidate_research"]["performance"]["approved"]["trades"] = 30
        a["candidate_research"]["performance"]["baseline"]["trades"] = 50
        r = gate.evaluate(a)
        self.assertIn("INSUFFICIENT_APPROVED_SAMPLE", r.blockers)
        self.assertIn("INSUFFICIENT_BASELINE_SAMPLE", r.blockers)
        self.assertNotEqual(self._run(a), 0)

    def test_concentration_breadth_horizon_and_cost_stress_block(self):
        cr = lambda a: a["candidate_research"]  # noqa: E731
        cases = {
            "SINGLE_SYMBOL_DOMINATES": lambda a: cr(a)["concentration"]["approved_by_symbol"].update(top_share_of_positive_r=0.8),
            "TOO_FEW_SYMBOLS_CONTRIBUTING": lambda a: cr(a)["concentration"]["approved_by_symbol"].update(groups_positive=1),
            "SINGLE_PERIOD_DOMINATES": lambda a: cr(a)["concentration"]["approved_by_month"].update(top_share_of_positive_r=0.9),
            "HISTORY_HORIZON_TOO_SHORT": lambda a: a["symbols"][0].update(history_days=26.0),
            "SYMBOLS_UNAVAILABLE": lambda a: a["symbols"][0].update(error="insufficient_history"),
            "COST_STRESS_FAILS_SLIPPAGE_X2": lambda a: cr(a)["cost_stress_approved"]["slippage_x2"].update(net_expectancy_r=-0.01),
            "TEMPORAL_ROBUSTNESS_INSUFFICIENT": lambda a: cr(a)["temporal_folds"].update(folds_positive_approved_expectancy=1),
            "INSUFFICIENT_UNIQUE_TEMPORAL_BLOCKS": lambda a: cr(a)["effective_sample"]["approved"].update(unique_blocks=12),
            "INSUFFICIENT_EFFECTIVE_SAMPLE": lambda a: cr(a)["effective_sample"]["approved"].update(effective_n=40.0),
            "UPLIFT_BLOCK_CI_NOT_POSITIVE": lambda a: cr(a)["inference"].update(uplift_vs_baseline=_dep(0.03, 0.2, -0.01, 0.2, 0.04, 0.2)),
            "PORTFOLIO_DRAWDOWN_EXCEEDS_RESEARCH_LIMIT": lambda a: a["portfolio_replay"].update(portfolio_max_drawdown=0.4),
            "PORTFOLIO_WALK_FORWARD_NOT_POSITIVE": lambda a: a["portfolio_replay"]["walk_forward"].update(folds_positive=2),
            "PORTFOLIO_ROBUSTNESS_NOT_ESTIMABLE": lambda a: a["portfolio_replay"].update(walk_forward={}),
            "PORTFOLIO_REALIZED_RETURN_NOT_POSITIVE": lambda a: a["portfolio_replay"].update(realized_return=-0.01),
            "PORTFOLIO_CENSORING_MATERIAL": lambda a: a["portfolio_replay"]["end_state"].update(portfolio_censoring_material=True),
            "CENSORING_MATERIAL": lambda a: cr(a)["censoring"].update(censoring_material=True),
            "CENSORING_REPORT_MISSING": lambda a: cr(a).pop("censoring"),
            "INSUFFICIENT_RESAMPLING_BLOCKS": lambda a: cr(a)["sample_adequacy"].update(resampling_blocks_approved=12),
            "POLICY_CONTENT_MISMATCH_OR_INVALID": lambda a: a.update(policy_parity="POLICY_CONTENT_MISMATCH"),
            "INSUFFICIENT_PORTFOLIO_TRADES": lambda a: a["portfolio_replay"].update(total_trades=10),
            "TOO_FEW_PORTFOLIO_SYMBOLS_CONTRIBUTING": lambda a: a["portfolio_replay"].update(contributing_symbols=1),
        }
        for blocker, mutate in cases.items():
            a = copy.deepcopy(_passing_artifact())
            mutate(a)
            with self.subTest(blocker=blocker):
                self.assertIn(blocker, gate.evaluate(a).blockers)

    def test_missing_evidence_sections_fail_closed(self):
        for key in ("candidate_research", "portfolio_replay", "methodology", "replay_parity",
                    "replay_policy_manifest"):
            a = _passing_artifact()
            del a[key]
            with self.subTest(key=key):
                self.assertFalse(gate.evaluate(a).promote)

    def test_negative_portfolio_expectancy_blocks(self):
        a = _passing_artifact()
        a["portfolio_replay"]["net_expectancy_r"] = -0.05
        a["portfolio_replay"]["ending_equity"] = 950.0
        r = gate.evaluate(a)
        self.assertIn("PORTFOLIO_EXPECTANCY_NOT_POSITIVE", r.blockers)
        self.assertIn("PORTFOLIO_FINAL_EQUITY_NOT_ABOVE_START", r.blockers)
        self.assertNotEqual(self._run(a), 0)

    def test_positive_candidate_edge_with_negative_portfolio_blocks(self):
        a = _passing_artifact()  # every candidate-layer condition passes
        a["portfolio_replay"]["ending_equity"] = 990.0
        r = gate.evaluate(a)
        self.assertFalse(r.promote)
        self.assertEqual(r.blockers, ["PORTFOLIO_FINAL_EQUITY_NOT_ABOVE_START"])

    def test_latest_legacy_evidence_is_blocked(self):
        r = gate.evaluate(copy.deepcopy(LEGACY_70B00B4))
        self.assertFalse(r.promote)
        for blocker in ("AUTHORITY_MODEL_UNSUPPORTED", "STATUS_NOT_AI_EDGE_PROVEN",
                        "APPROVED_EXPECTANCY_NOT_POSITIVE", "UPLIFT_BLOCK_CI_NOT_POSITIVE",
                        "APPROVED_EXPECTANCY_BLOCK_CI_NOT_POSITIVE",
                        "HISTORICAL_CONTEXT_PARITY_INCOMPLETE"):
            self.assertIn(blocker, r.blockers)
        # The legacy IID blocker is ignored, not propagated.
        self.assertNotIn("UPLIFT_NOT_STATISTICALLY_POSITIVE", r.blockers)


class IidHasZeroPromotionAuthority(unittest.TestCase):
    """P0-STAT-01/02: IID can make neither PROMOTE->BLOCK nor BLOCK->PROMOTE."""

    def test_negative_iid_and_legacy_report_cannot_block_a_promotion(self):
        a = _passing_artifact()
        for key in ("approved_expectancy", "uplift_vs_baseline"):
            a["candidate_research"]["inference"][key]["iid_ci"] = [-9.0, -8.0]
        a["report"]["bootstrap_ci_low_r"] = -1.0
        a["report"]["nexus_expectancy_r"] = -1.0
        a["legacy_diagnostic"] = {"status": "AI_EDGE_NOT_PROVEN",
                                  "blockers": ["UPLIFT_NOT_STATISTICALLY_POSITIVE"]}
        a["blockers"] = ["UPLIFT_NOT_STATISTICALLY_POSITIVE"]   # legacy code leaked
        a["portfolio_replay"]["approximate_trade_level_ci"]["authority_ci_low"] = -9.0
        r = gate.evaluate(a)
        self.assertTrue(r.promote, r.blockers)
        self.assertEqual(self._code(a), gate.EXIT_PROMOTE)

    def test_positive_iid_and_legacy_report_cannot_promote_a_block(self):
        a = _passing_artifact()
        a["candidate_research"]["inference"]["uplift_vs_baseline"] = _dep(
            -0.05, 0.2, -0.04, 0.2, -0.06, 0.2, iid=(0.5, 0.9))
        a["candidate_research"]["inference"]["approved_expectancy"] = _dep(
            -0.01, 0.3, 0.04, 0.3, 0.02, 0.3, iid=(0.5, 0.9))
        a["report"]["bootstrap_ci_low_r"] = 0.9
        a["legacy_diagnostic"] = {"status": "AI_EDGE_PROVEN", "blockers": []}
        r = gate.evaluate(a)
        self.assertFalse(r.promote)
        self.assertIn("UPLIFT_BLOCK_CI_NOT_POSITIVE", r.blockers)
        self.assertIn("APPROVED_EXPECTANCY_BLOCK_CI_NOT_POSITIVE", r.blockers)
        self.assertNotEqual(self._code(a), 0)

    def test_gate_recomputes_authority_from_blocks_only(self):
        a = _passing_artifact()
        sec = a["candidate_research"]["inference"]["approved_expectancy"]
        # A tampered authority that silently used IID (or anything else).
        sec["authority_ci_low"] = 0.2
        r = gate.evaluate(a)
        self.assertIn("APPROVED_EXPECTANCY_AUTHORITY_CI_INCONSISTENT", r.blockers)
        self.assertEqual(gate.block_only_authority(sec, required_ms=DAY, min_blocks=30),
                         (0.04, 0.32, "VALID"))

    def test_no_valid_block_interval_fails_closed(self):
        a = _passing_artifact()
        sec = a["candidate_research"]["inference"]["approved_expectancy"]
        for iv in sec["block_intervals"]:
            iv["ci"] = [None, None]
        sec["iid_ci"] = [0.5, 0.9]
        r = gate.evaluate(a)
        self.assertIn("APPROVED_EXPECTANCY_BLOCK_CI_NOT_POSITIVE", r.blockers)

    def test_gate_source_never_reads_iid(self):
        import inspect
        src = inspect.getsource(gate.evaluate) + inspect.getsource(gate.block_only_authority)
        self.assertNotIn('"iid_ci"', src)
        self.assertNotIn("bootstrap_ci_low_r", src)
        self.assertNotIn('artifact.get("report")', src)

    def _code(self, a):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.json"
            path.write_text(json.dumps(a), encoding="utf-8")
            return gate.main([str(path)])


if __name__ == "__main__":
    unittest.main()
