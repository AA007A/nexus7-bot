"""Phase 6 fold independence, residual dependence and the staged gates.

1. Candidate fold K never shares outcome market time with fold K+1.
2. A portfolio fold never carries an open trade into the next fold's period.
3. Long trades increase the embargo automatically.
4. Four folds that do not fit -> INSUFFICIENT_INDEPENDENT_PORTFOLIO_PERIODS.
5. The fold count is never increased by shrinking the embargo.
6. The LIVE_RELEASE_GATE cannot pass without source-authenticated evidence
   (see tests/test_live_release_evidence.py for the full Stage-C contract).
7. The research gate may report LIVE_POLICY_OBSERVATION_PENDING without
   claiming production readiness.
8. A successful research gate never yields PRODUCTION_READY = YES.
9. Significant residual dependence at the longest usable block fails closed.
"""
import copy
import io
import json
import math
import random
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from bot import nexus_oos_inference as inf
from bot import nexus_oos_portfolio_engine as pe
from bot import nexus_oos_promotion_gate as gate
from bot import nexus_oos_real_replay as replay
from tests.test_nexus_oos_censoring_and_horizon import INSTR, M15, MMR, _manifest
from tests.test_nexus_oos_promotion_gate import _passing_artifact

DAY = inf.DAY_MS
HOUR = 3_600_000
D0 = 1_735_689_600_000
SYMS = ("BTCUSDT", "XRPUSDT", "LINKUSDT")


def _cand_rows(days=150, per_day=4, long_every=17, long_ms=40 * HOUR, seed=3):
    """Executable candidate rows; every ``long_every``-th row is a long trade."""
    rng = random.Random(seed)
    rows = []
    for d in range(days):
        for k in range(per_day):
            i = d * per_day + k
            ts = D0 + d * DAY + k * 6 * HOUR
            horizon = long_ms if i % long_every == 0 else 3 * HOUR
            rows.append({"ts": ts, "symbol": SYMS[i % 3], "approved": i % 2 == 0,
                         "r": rng.gauss(0.05, 0.8), "outcome_status": "RESOLVED",
                         "outcome_end_ts": ts + horizon})
    rows.append({"ts": D0 + 10 * DAY + HOUR, "symbol": "BTCUSDT", "approved": True, "r": None,
                 "outcome_status": "RIGHT_CENSORED_RESEARCH_LIMIT", "outcome_end_ts": None})
    return rows


def _trade(ts, *, hold_ms=HOUR, symbol="BTCUSDT", win=True):
    return {"ts": ts, "symbol": symbol, "direction": "LONG", "approved": True, "executable": True,
            "strategy_score": 70, "score_adjusted": 70, "rr": 3.0, "signal_entry": 100.0,
            "sl": 99.0, "tp": 103.0, "fill": 100.0,
            "legs": [(ts + hold_ms, 1.0, 101.0 if win else 99.5, "X")],
            "marks": [(ts + k * M15, 100.0) for k in range(1, 4)], "funding": [],
            "fee_rate": 0.0006, "cost_fraction": 0.0022, "outcome_status": "RESOLVED",
            "month": "2025-01"}


def _censored(ts, until_ms):
    marks = [(ts + k * HOUR, 100.5) for k in range(1, until_ms // HOUR)]
    return {"ts": ts, "symbol": "XRPUSDT", "direction": "LONG", "approved": True,
            "executable": True, "strategy_score": 70, "score_adjusted": 70, "rr": 3.0,
            "signal_entry": 100.0, "sl": 99.0, "tp": 103.0, "fill": 100.0, "legs": [],
            "marks": marks, "funding": [], "fee_rate": 0.0006, "cost_fraction": 0.0022,
            "outcome_status": "RIGHT_CENSORED_DATA_END", "censor_ts": marks[-1][0],
            "native_sl_at_censor": 99.0, "native_tp": 103.0, "month": "2025-01"}


class CandidateFoldsDoNotShareMarketTime(unittest.TestCase):
    def test_fold_k_outcomes_end_before_fold_k1_decisions(self):
        rows = _cand_rows()
        req = inf.required_block_ms(rows)
        self.assertEqual(req, 2 * DAY)                      # 40 h -> 2 whole days
        tf = replay.temporal_folds(rows, required_horizon_ms=req)
        self.assertEqual(tf["status"], "OK")
        self.assertEqual(tf["folds_total"], 4)
        self.assertTrue(tf["folds_overlap_free"])
        self.assertGreaterEqual(tf["fold_embargo_ms"], req)
        for a, b in zip(tf["folds"], tf["folds"][1:]):
            kept_end = max(int(r["outcome_end_ts"]) for r in rows
                           if r["outcome_status"] == "RESOLVED"
                           and a["decision_start_ts"] <= r["ts"] < a["decision_end_ts"]
                           and r["outcome_end_ts"] < a["outcome_window_end_ts"])
            self.assertLess(kept_end, b["decision_start_ts"])
            self.assertLess(a["max_market_ts_used"], b["decision_start_ts"])
            self.assertGreaterEqual(b["decision_start_ts"] - a["decision_end_ts"],
                                    tf["fold_required_horizon_ms"] + tf["fold_embargo_ms"])
        for f in tf["folds"]:
            for key in ("decision_start_ts", "decision_end_ts", "eligible_decisions",
                        "purged_boundary_decisions", "resolved_approved_trades",
                        "censored_excluded", "approved_mean_r", "baseline_mean_r", "uplift_r",
                        "symbols_represented"):
                self.assertIn(key, f)
        self.assertEqual(sum(f["censored_excluded"] for f in tf["folds"]), 1)
        ok, why = gate.folds_independent(tf, required_ms=req)
        self.assertTrue(ok, why)

    def test_overlap_is_detected_and_blocks_the_gate(self):
        a = _passing_artifact()
        f = a["candidate_research"]["temporal_folds"]["folds"]
        f[0]["max_market_ts_used"] = f[1]["decision_start_ts"] + 1   # shares market time
        with self.assertRaises(AssertionError):
            inf.assert_folds_overlap_free(f)
        r = gate.evaluate(a)
        self.assertIn("TEMPORAL_FOLDS_NOT_INDEPENDENT", r.blockers)
        self.assertIn("TEMPORAL_ROBUSTNESS_INSUFFICIENT", r.blockers)
        self.assertFalse(r.promote)

    def test_flag_alone_is_not_trusted(self):
        a = _passing_artifact()
        wf = a["portfolio_replay"]["walk_forward"]
        wf["folds"][2]["decision_start_ts"] = wf["folds"][1]["decision_end_ts"]  # no gap
        self.assertTrue(wf["folds_overlap_free"])
        self.assertIn("PORTFOLIO_FOLDS_NOT_INDEPENDENT", gate.evaluate(a).blockers)


class PortfolioFoldsCarryNothingForward(unittest.TestCase):
    def test_open_trade_is_cut_at_its_own_outcome_window(self):
        rows = [_trade(D0 + d * DAY, symbol=SYMS[d % 3], win=d % 3 != 0) for d in range(0, 120, 2)]
        lay = inf.purged_calendar_folds(D0, rows[-1]["ts"] + 1, required_horizon_ms=DAY)
        late = lay["windows"][0]["decision_end_ts"] - 2 * HOUR
        rows.append(_censored(late, 60 * DAY))                      # would run 60 days
        wf = pe.walk_forward_folds(rows, _manifest(), instruments=INSTR, mmr_proxy=MMR)
        self.assertEqual(wf["status"], "OK")
        self.assertTrue(wf["folds_overlap_free"])
        first, second = wf["folds"][0], wf["folds"][1]
        self.assertEqual(first["open_positions_at_end"], 1)
        self.assertLess(first["max_market_ts_used"], first["outcome_window_end_ts"])
        self.assertLess(first["max_market_ts_used"], second["decision_start_ts"])
        self.assertEqual(wf["fold_account_state"], "RESET_FOR_REGIME_ROBUSTNESS")
        self.assertTrue(wf["not_a_continuous_backtest"])
        self.assertTrue(gate.folds_independent(wf, required_ms=DAY)[0])


class EmbargoGrowsWithTradeLength(unittest.TestCase):
    def test_long_trade_raises_embargo(self):
        short = [_trade(D0 + d * DAY, symbol=SYMS[d % 3]) for d in range(0, 150, 2)]
        wf_s = pe.walk_forward_folds(short, _manifest(), instruments=INSTR, mmr_proxy=MMR)
        long_ = copy.deepcopy(short)
        long_[5] = _trade(long_[5]["ts"], hold_ms=5 * DAY + HOUR)
        wf_l = pe.walk_forward_folds(long_, _manifest(), instruments=INSTR, mmr_proxy=MMR)
        self.assertEqual(wf_s["fold_embargo_ms"], DAY)
        self.assertEqual(wf_l["fold_embargo_ms"], 6 * DAY)
        self.assertEqual(wf_l["fold_required_horizon_ms"], 6 * DAY)
        self.assertLess(wf_l["layout"]["fold_decision_window_days"],
                        wf_s["layout"]["fold_decision_window_days"])
        # Candidate side: the required horizon follows the longest resolved trade.
        self.assertEqual(inf.required_block_ms(_cand_rows(long_ms=5 * DAY + HOUR)), 6 * DAY)

    def test_gate_rejects_embargo_below_its_own_recomputed_horizon(self):
        a = _passing_artifact()
        a["candidate_research"]["inference"]["outcome_horizon_resolved_executable"]["max_ms"] = 3 * DAY
        a["candidate_research"]["inference"]["required_block_ms"] = 3 * DAY
        r = gate.evaluate(a)                                   # folds were laid out for 1 day
        self.assertIn("TEMPORAL_FOLDS_NOT_INDEPENDENT", r.blockers)
        self.assertIn("PORTFOLIO_FOLDS_NOT_INDEPENDENT", r.blockers)


class InsufficientIndependentPeriods(unittest.TestCase):
    def test_four_folds_that_do_not_fit_are_reported_insufficient(self):
        lay = inf.purged_calendar_folds(D0, D0 + 60 * DAY, required_horizon_ms=3 * DAY)
        self.assertEqual(lay["status"], inf.FOLDS_INSUFFICIENT)
        self.assertEqual(lay["windows"], [])
        rows = [_trade(D0 + d * DAY, hold_ms=3 * DAY - HOUR) for d in range(0, 60, 2)]
        wf = pe.walk_forward_folds(rows, _manifest(), instruments=INSTR, mmr_proxy=MMR)
        self.assertEqual(wf["status"], "INSUFFICIENT_INDEPENDENT_PORTFOLIO_PERIODS")
        self.assertEqual((wf["folds_total"], wf["folds_positive"]), (0, 0))
        a = _passing_artifact()
        a["portfolio_replay"]["walk_forward"] = wf
        blockers = gate.evaluate(a).blockers
        self.assertIn("INSUFFICIENT_INDEPENDENT_PORTFOLIO_PERIODS", blockers)
        self.assertIn("PORTFOLIO_FOLDS_NOT_INDEPENDENT", blockers)
        tf = replay.temporal_folds(_cand_rows(days=40), required_horizon_ms=2 * DAY)
        self.assertEqual(tf["status"], inf.FOLDS_INSUFFICIENT)
        a = _passing_artifact()
        a["candidate_research"]["temporal_folds"] = tf
        self.assertIn("INSUFFICIENT_INDEPENDENT_TEMPORAL_FOLDS", gate.evaluate(a).blockers)


class EmbargoIsNeverShrunk(unittest.TestCase):
    def test_smaller_embargo_is_refused(self):
        with self.assertRaises(ValueError):
            inf.purged_calendar_folds(D0, D0 + 365 * DAY, required_horizon_ms=5 * DAY,
                                      embargo_ms=DAY)

    def test_fold_count_is_fixed_and_width_only_shrinks(self):
        widths = []
        for emb_days in (5, 7, 10, 14):
            lay = inf.purged_calendar_folds(D0, D0 + 180 * DAY, required_horizon_ms=5 * DAY,
                                            embargo_ms=emb_days * DAY)
            self.assertEqual(lay["folds_requested"], inf.FOLD_COUNT)
            if lay["status"] == "OK":
                self.assertEqual(len(lay["windows"]), inf.FOLD_COUNT)
            widths.append(lay["fold_decision_window_days"])
        self.assertEqual(widths, sorted(widths, reverse=True))

    def test_tampered_small_embargo_is_blocked(self):
        a = _passing_artifact()
        for sec in (a["candidate_research"]["temporal_folds"], a["portfolio_replay"]["walk_forward"]):
            sec["fold_embargo_ms"] = DAY // 2
        r = gate.evaluate(a)
        self.assertIn("TEMPORAL_FOLDS_NOT_INDEPENDENT", r.blockers)
        self.assertIn("PORTFOLIO_FOLDS_NOT_INDEPENDENT", r.blockers)


class StagedGates(unittest.TestCase):
    def test_live_gate_blocks_without_authenticated_evidence(self):
        a = _passing_artifact()
        for status in (gate.POLICY_OBSERVATION_PENDING, gate.POLICY_CONTENT_MATCH):
            b = copy.deepcopy(a)
            b["policy_parity"] = status
            r = gate.evaluate_live(b, None)
            self.assertFalse(r.promote)
            self.assertIn("LIVE_PROVENANCE_SOURCE_UNAVAILABLE", r.blockers)
            d = r.to_dict()
            self.assertFalse(d["production_ready"])
            # Stage C never evaluates a caller-supplied artifact.
            self.assertEqual(d["stages"]["RESEARCH_PROMOTION"], "BLOCK")
            self.assertIn("TRUSTED_RESEARCH_ARTIFACT_UNAVAILABLE", r.blockers)
            self.assertEqual(d["stages"]["LIVE_RELEASE_PRECONDITIONS"], "BLOCK")
            self.assertEqual(d["stages"]["REAL_ORDER_ENABLEMENT"], "HUMAN_ACTION_REQUIRED")

    def test_research_gate_passes_with_pending_observation_without_readiness(self):
        a = _passing_artifact()
        a["policy_parity"] = gate.POLICY_OBSERVATION_PENDING
        r = gate.evaluate(a)
        self.assertTrue(r.promote, r.blockers)
        d = r.to_dict()
        self.assertEqual(d["gate"], gate.RESEARCH_GATE)
        self.assertEqual(d["verdict"], "PASS")
        self.assertEqual(d["policy_content"], "LIVE_POLICY_OBSERVATION_PENDING")
        self.assertEqual(d["stages"]["RESEARCH_PROMOTION"], "PASS")
        self.assertFalse(d["production_ready"])

    def test_successful_research_gate_never_production_ready(self):
        for status in (gate.POLICY_CONTENT_MATCH, gate.POLICY_OBSERVATION_PENDING):
            a = _passing_artifact()
            a["policy_parity"] = status
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "a.json"
                path.write_text(json.dumps(a), encoding="utf-8")
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = gate.main([str(path), "--gate", "research"])
            out = json.loads(buf.getvalue())
            self.assertEqual(code, gate.EXIT_PROMOTE)
            self.assertEqual(out["verdict"], "PASS")
            self.assertIs(out["production_ready"], False)
            self.assertIs(out["authorizes_real_trading"], False)

    def test_live_cli_from_ci_never_passes(self):
        a = _passing_artifact()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.json"
            path.write_text(json.dumps(a), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                code = gate.main([str(path), "--gate", "live"])
        self.assertNotEqual(code, gate.EXIT_PROMOTE)


class ResidualDependenceFailsClosed(unittest.TestCase):
    def _persistent_rows(self, days=120):
        rng = random.Random(7)
        rows = []
        for d in range(days):
            level = math.sin(2 * math.pi * d / 60.0)              # slow regime: spills across blocks
            for k in range(4):
                ts = D0 + d * DAY + k * 6 * HOUR
                rows.append({"ts": ts, "outcome_end_ts": ts + 3 * HOUR, "approved": k % 2 == 0,
                             "r": level + rng.gauss(0, 0.1), "outcome_status": "RESOLVED"})
        return rows

    def test_inference_withdraws_authority_and_never_substitutes_a_shorter_block(self):
        res = inf.dependence_aware_mean(self._persistent_rows(), samples=200)
        self.assertEqual(res["authority_status"], inf.RESIDUAL_DEPENDENCE)
        self.assertIsNone(res["authority_ci_low"])
        self.assertTrue(res["residual_dependence_at_longest_usable"]["significant"])
        self.assertFalse(any(iv["authoritative"] for iv in res["block_intervals"]))
        for iv in res["block_intervals"]:
            self.assertIn("resampling_blocks", iv)
            self.assertNotIn("independent_blocks", iv)
            if iv["block_ms"] >= res["required_block_ms"]:
                self.assertEqual(set(iv["residual_dependence"]["acf"]), {"1", "2", "3"})

    def test_gate_fails_closed_on_dependence_at_longest_usable_block(self):
        from tests.test_nexus_oos_promotion_gate import residual
        a = _passing_artifact()
        sec = a["candidate_research"]["inference"]["uplift_vs_baseline"]
        longest = max(sec["block_intervals"], key=lambda iv: iv["block_ms"])
        n = longest["resampling_blocks"]
        # Persistent regime across blocks; shorter blocks stay clean.
        longest["residual_dependence"] = residual(
            [round(math.sin(2 * math.pi * i / 20.0), 12) for i in range(n)])
        r = gate.evaluate(a)
        self.assertFalse(r.promote)
        self.assertIn("RESIDUAL_DEPENDENCE_AT_LONGEST_USABLE_BLOCK", r.blockers)
        self.assertIn("UPLIFT_BLOCK_CI_NOT_POSITIVE", r.blockers)

    def test_gate_recomputes_from_series_and_rejects_tampered_acf(self):
        from tests.test_nexus_oos_promotion_gate import residual
        a = _passing_artifact()
        sec = a["candidate_research"]["inference"]["uplift_vs_baseline"]
        longest = max(sec["block_intervals"], key=lambda iv: iv["block_ms"])
        n = longest["resampling_blocks"]
        rd = residual([round(math.sin(2 * math.pi * i / 20.0), 12) for i in range(n)])
        rd["acf"] = {"1": 0.01, "2": 0.0, "3": -0.01}   # stored values claim "clean"
        rd["significant"] = False
        longest["residual_dependence"] = rd
        self.assertIn("RESIDUAL_DEPENDENCE_SERIES_INCONSISTENT", gate.evaluate(a).blockers)
        del rd["series"]
        self.assertIn("RESIDUAL_DEPENDENCE_SERIES_MISSING", gate.evaluate(a).blockers)
        del longest["residual_dependence"]
        self.assertIn("RESIDUAL_DEPENDENCE_SERIES_MISSING", gate.evaluate(a).blockers)

    def test_inference_series_reproduces_reported_acf(self):
        res = inf.dependence_aware_mean(self._persistent_rows(), samples=200)
        for iv in res["block_intervals"]:
            rd = iv["residual_dependence"]
            if rd is None:
                continue
            self.assertEqual(rd["n_blocks"], iv["resampling_blocks"])
            again = inf.acf_from_block_series(rd["series"]["block_ids"], rd["series"]["means"])
            self.assertEqual(again["acf"], rd["acf"])
            self.assertEqual(len(rd["series"]["counts"]), rd["n_blocks"])


class CalendarResidualACF(unittest.TestCase):
    """11-14: residual ACF uses CALENDAR block ids; the gate recomputes from the series."""

    def _gate_with_series(self, block_ids, means, counts=None, n_override=None):
        from tests.test_nexus_oos_promotion_gate import residual
        a = _passing_artifact()
        sec = a["candidate_research"]["inference"]["uplift_vs_baseline"]
        longest = max(sec["block_intervals"], key=lambda iv: iv["block_ms"])
        longest["residual_dependence"] = residual(means, block_ids=block_ids, counts=counts)
        longest["resampling_blocks"] = n_override if n_override is not None else len(means)
        return gate.evaluate(a).blockers

    def test_11_missing_block_ids_break_authority(self):
        from tests.test_nexus_oos_promotion_gate import clean_series
        m = clean_series(45)
        a = _passing_artifact()
        sec = a["candidate_research"]["inference"]["uplift_vs_baseline"]
        longest = max(sec["block_intervals"], key=lambda iv: iv["block_ms"])
        del longest["residual_dependence"]["series"]["block_ids"]
        self.assertIn("RESIDUAL_DEPENDENCE_SERIES_MISSING", gate.evaluate(a).blockers)
        # Arrays of different length also fail closed.
        blockers = self._gate_with_series(list(range(44)), m)
        self.assertIn("RESIDUAL_DEPENDENCE_SERIES_INCONSISTENT", blockers)
        self.assertIn("UPLIFT_BLOCK_CI_NOT_POSITIVE", blockers)

    def test_12_duplicate_or_out_of_order_ids_break_authority(self):
        from tests.test_nexus_oos_promotion_gate import clean_series
        m = clean_series(45)
        ids = list(range(100, 145))
        dup = ids[:10] + [ids[9]] + ids[11:]
        swapped = ids[:5] + [ids[6], ids[5]] + ids[7:]
        for bad in (dup, swapped, [float(i) for i in ids], [True] + ids[1:]):
            self.assertIn("RESIDUAL_DEPENDENCE_SERIES_INCONSISTENT", self._gate_with_series(bad, m))
        self.assertIn("RESIDUAL_DEPENDENCE_SERIES_INCONSISTENT",
                      self._gate_with_series(ids, m, counts=[3] * 44 + [0]))
        self.assertIn("RESIDUAL_DEPENDENCE_SERIES_INCONSISTENT",
                      self._gate_with_series(ids, m, n_override=46))

    def test_13_missing_calendar_blocks_are_not_lag1_pairs(self):
        res = inf.acf_from_block_series([100, 101, 105], [0.1, 0.2, 0.3], min_pairs=1)
        self.assertEqual(res["lags"]["1"]["eligible_pairs"], 1)          # 100->101 only
        self.assertEqual(res["lags"]["3"]["eligible_pairs"], 0)
        self.assertEqual(res["lags"]["2"]["eligible_pairs"], 0)
        # Every other calendar block missing: no lag-1 or lag-3 pairs at all.
        ids = list(range(0, 120, 2))
        vals = [math.sin(i / 3.0) for i in range(60)]
        res = inf.acf_from_block_series(ids, vals)
        self.assertEqual(res["lags"]["1"]["eligible_pairs"], 0)
        self.assertEqual(res["lags"]["2"]["eligible_pairs"], 59)
        self.assertIsNone(res["lags"]["1"]["acf"])
        # Positional (wrong) ACF would have used 59 lag-1 pairs.
        for k, v in res["lags"].items():
            self.assertEqual(set(v), {"eligible_pairs", "acf", "reference_band", "significant"})

    def test_14_too_few_calendar_pairs_is_insufficient_evidence(self):
        from tests.test_nexus_oos_promotion_gate import clean_series
        m = clean_series(45)
        sparse = list(range(0, 90, 2))                 # 45 blocks, no lag-1 / lag-3 neighbours
        blockers = self._gate_with_series(sparse, m)
        self.assertIn(inf.RESIDUAL_NOT_ESTIMABLE, blockers)
        self.assertIn("UPLIFT_BLOCK_CI_NOT_POSITIVE", blockers)
        res = inf.acf_from_block_series(sparse, m)
        self.assertTrue(res["insufficient_pairs"])
        self.assertIsNone(res["significant"])
        self.assertLess(res["lags"]["1"]["eligible_pairs"], inf.MIN_RESIDUAL_ACF_PAIRS)


if __name__ == "__main__":
    unittest.main()
