"""Right-censoring, horizon-aware authority, walk-forward robustness and live
policy attestation (statistical-validity P0s of the replay-parity phase)."""
import copy
import json
import os
import random
import unittest
from unittest.mock import patch

from bot import nexus_oos_execution_parity as xp
from bot import nexus_oos_inference as inf
from bot import nexus_oos_portfolio_engine as pe
from bot import nexus_oos_promotion_gate as gate
from bot import nexus_oos_replay_manifest as rm
from bot import policy_attestation as pa
from bot import nexus_oos_real_replay as replay

D0 = 1_760_054_400_000
M15 = 15 * 60 * 1000
H = 4 * M15
DAY = inf.DAY_MS


def _bars(ohlc, start=D0):
    return [{"ts": start + i * M15, "o": o, "h": h, "l": l, "c": c} for i, (o, h, l, c) in enumerate(ohlc)]


def _manifest(**over):
    raw = json.loads(rm.DEFAULT_PATH.read_text(encoding="utf-8"))
    for k, v in over.items():
        raw["values"][k]["value"] = v
    return rm.ReplayManifest(raw, source="test")


INFO = {"minQty": 1.0, "lotSize": 1.0, "qtyStep": 1.0, "multiplier": 0.01, "minNotional": 0.0,
        "tickSize": 0.01, "contractMaintainMarginReference": 0.004}
INSTR = {s: dict(INFO) for s in ("BTCUSDT", "XRPUSDT", "LINKUSDT")}
MMR = {s: 0.004 for s in INSTR}


def _net(sim, direction="LONG"):
    return xp.legs_net_r(sim, direction=direction, fee_rate=0.0006, funding_events=[],
                         price_at_ts=None, planned_risk_fraction=0.01, slippage_rate=0.0)


class CensoredOutcomesAreNotTrades(unittest.TestCase):
    def test_data_end_is_never_a_realized_trade(self):
        sim = xp.simulate_production_exit(
            direction="LONG", bars=_bars([(100.0, 100.3, 99.9, 100.2)] * 5), start_idx=0,
            signal_entry=100.0, signal_sl=99.0, signal_tp=110.0, slippage_rate=0.0)
        self.assertEqual(sim["outcome_status"], xp.RIGHT_CENSORED_DATA_END)
        self.assertEqual(sim["legs"], [])
        n = _net(sim)
        self.assertIsNone(n["r"])
        self.assertAlmostEqual(n["marked_r"], (0.2 / 100 - 0.0006) / 0.01)  # entry fee only
        self.assertLess(n["bound_worst_r"], n["marked_r"])
        self.assertGreater(n["bound_best_r"], n["marked_r"])

    def test_research_limit_is_never_a_realized_trade(self):
        sim = xp.simulate_production_exit(
            direction="LONG", bars=_bars([(100.0, 100.3, 99.9, 100.2)] * 50), start_idx=0,
            signal_entry=100.0, signal_sl=99.0, signal_tp=110.0, slippage_rate=0.0,
            policy=xp.ExitPolicy(research_max_hold_bars=10))
        self.assertEqual(sim["outcome_status"], xp.RIGHT_CENSORED_RESEARCH_LIMIT)
        self.assertEqual(sim["censor_ts"], D0 + 10 * M15)
        self.assertIsNone(_net(sim)["r"])

    def test_candidate_statistics_exclude_censored_rows(self):
        rows = [{"ts": D0 + i * 8 * H, "outcome_end_ts": D0 + i * 8 * H + H, "r": -1.0,
                 "outcome_status": "RESOLVED", "approved": True} for i in range(120)]
        cens = [{"ts": D0 + i * 8 * H + M15, "outcome_end_ts": D0 + 200 * DAY, "r": 50.0,
                 "outcome_status": "RIGHT_CENSORED_RESEARCH_LIMIT", "censored": "x", "approved": True}
                for i in range(120)]
        d = inf.dependence_aware_mean(rows + cens, lambda r: r["approved"], samples=200)
        self.assertEqual(d["n"], 120)
        self.assertEqual(d["mean_r"], -1.0)
        self.assertEqual(inf.outcome_horizon_stats(rows + cens)["max_ms"], H)

    def test_censoring_materiality_rule(self):
        base = {"executable": True, "approved": True, "symbol": "X", "direction": "LONG",
                "production_regime": "R"}
        resolved = [{**base, "outcome_status": "RESOLVED", "r": 0.1, "bound_worst_r": 0.1,
                     "bound_best_r": 0.1} for _ in range(95)]
        cens = [{**base, "outcome_status": "RIGHT_CENSORED_RESEARCH_LIMIT", "r": None,
                 "bound_worst_r": -5.0, "bound_best_r": 2.0, "marked_r": 0.3} for _ in range(5)]
        rep = replay.censoring_report(resolved + cens)
        self.assertEqual(rep["censored_count"], 5)
        self.assertTrue(rep["censoring_material"])     # worst bound flips the sign
        mild = [{**c, "bound_worst_r": 0.1, "bound_best_r": 0.1} for c in cens]
        self.assertFalse(replay.censoring_report(resolved + mild)["censoring_material"])
        many = resolved[:50] + [{**c, "bound_worst_r": 0.2} for c in cens] * 4
        self.assertTrue(replay.censoring_report(many)["censoring_material"])   # rate > 5%
        self.assertEqual(rep["marked_r_role"], "DIAGNOSTIC_ONLY")


class PortfolioEndStateKeepsUnresolvedPositionsOpen(unittest.TestCase):
    def _censored_row(self):
        return {"ts": D0, "symbol": "BTCUSDT", "direction": "LONG", "approved": True,
                "executable": True, "strategy_score": 70, "score_adjusted": 70, "rr": 3.0,
                "signal_entry": 100.0, "sl": 99.0, "tp": 103.0, "fill": 100.0, "legs": [],
                "marks": [(D0 + k * M15, 100.5) for k in range(1, 9)], "funding": [],
                "fee_rate": 0.0006, "cost_fraction": 0.0022,
                "outcome_status": "RIGHT_CENSORED_DATA_END", "censor_ts": D0 + 8 * M15,
                "native_sl_at_censor": 99.0, "native_tp": 103.0, "month": "2025-10"}

    def test_no_forced_close_no_artificial_exit_fee(self):
        out = pe.run_portfolio([self._censored_row()], _manifest(), instruments=INSTR, mmr_proxy=MMR)
        es = out["end_state"]
        self.assertEqual(out["total_trades"], 0)
        self.assertEqual(es["open_positions_at_end"], 1)
        qty = es["unrealized_pnl_at_end"] / 0.5
        entry_fee = 0.0006 * 100.0 * qty
        self.assertAlmostEqual(es["realized_pnl"], -entry_fee, places=9)      # entry fee only
        self.assertAlmostEqual(es["marked_final_equity"], es["realized_equity_end"] + es["unrealized_pnl_at_end"])
        self.assertAlmostEqual(out["ending_equity"], es["marked_final_equity"])
        self.assertLess(es["final_equity_worst_bound"], es["marked_final_equity"])
        self.assertGreater(es["final_equity_best_bound"], es["marked_final_equity"])
        self.assertTrue(es["portfolio_censoring_material"])   # bounds straddle the start equity

    def test_censored_position_blocks_later_entries_and_is_material(self):
        later = {"ts": D0 + 4 * M15, "symbol": "XRPUSDT", "direction": "LONG", "approved": True,
                 "executable": True, "strategy_score": 70, "score_adjusted": 70, "rr": 3.0,
                 "signal_entry": 100.0, "sl": 99.0, "tp": 103.0, "fill": 100.0,
                 "legs": [(D0 + 6 * M15, 1.0, 101.0, "NATIVE_TP")],
                 "marks": [(D0 + 5 * M15, 100.5)], "funding": [], "fee_rate": 0.0006,
                 "cost_fraction": 0.0022, "outcome_status": "RESOLVED", "month": "2025-10"}
        row = self._censored_row()
        row["censor_ts"] = D0 + 2 * M15
        row["marks"] = row["marks"][:2]
        out = pe.run_portfolio([row, later], _manifest(), instruments=INSTR, mmr_proxy=MMR)
        self.assertEqual(out["skipped"].get("liquidation_guard_multi_position"), 1)
        self.assertTrue(out["end_state"]["censored_before_last_candidate"])
        self.assertTrue(out["end_state"]["portfolio_censoring_material"])

    def test_effective_live_concurrency_is_one(self):
        out = pe.run_portfolio([self._censored_row()], _manifest(), instruments=INSTR, mmr_proxy=MMR)
        self.assertEqual(out["effective_live_max_concurrent_positions"], 1)
        self.assertEqual(out["configured_max_positions"], 2)


class HorizonAwareAuthority(unittest.TestCase):
    def _rows(self, horizon_ms, days=60, per_day=6):
        rng = random.Random(4)
        rows = []
        for d in range(days):
            shock = rng.gauss(0.05, 0.5)
            for k in range(per_day):
                ts = D0 + d * DAY + k * 4 * 3_600_000
                rows.append({"ts": ts, "outcome_end_ts": ts + horizon_ms, "r": shock + rng.gauss(0, 0.2),
                             "approved": k % 2 == 0, "outcome_status": "RESOLVED"})
        return rows

    def test_blocks_shorter_than_outcome_horizon_have_no_authority(self):
        d = inf.dependence_aware_mean(self._rows(5 * DAY, days=200), lambda r: r["approved"], samples=300)
        self.assertEqual(d["required_block_days"], 5)
        for iv in d["block_intervals"]:
            if iv["block_ms"] < 5 * DAY:
                self.assertFalse(iv["authoritative"])
                self.assertEqual(iv["invalid_reason"], "AUTHORITY_BLOCK_SHORTER_THAN_OUTCOME_HORIZON")
        self.assertTrue(all(iv["block_ms"] >= 5 * DAY for iv in d["block_intervals"] if iv["authoritative"]))

    def test_insufficient_resampling_blocks_gives_no_authority_and_iid_cannot_substitute(self):
        d = inf.dependence_aware_mean(self._rows(10 * DAY, days=60), lambda r: r["approved"], samples=300)
        self.assertIsNone(d["authority_ci_low"])
        self.assertIsNone(d["authority_ci_high"])
        self.assertEqual(d["authority_status"], "INSUFFICIENT_RESAMPLING_BLOCKS")
        self.assertIsNotNone(d["iid_ci"][0])      # IID exists but has no authority

    def test_gate_rejects_short_blocks_against_long_horizon(self):
        from tests.test_nexus_oos_promotion_gate import _passing_artifact
        a = _passing_artifact()
        a["candidate_research"]["inference"]["outcome_horizon_resolved_executable"] = {"max_ms": 5 * DAY}
        r = gate.evaluate(a)
        self.assertFalse(r.promote)
        self.assertIn("AUTHORITY_BLOCK_SHORTER_THAN_OUTCOME_HORIZON", r.blockers)
        self.assertIn("APPROVED_EXPECTANCY_BLOCK_CI_NOT_POSITIVE", r.blockers)

    def test_gate_requires_minimum_resampling_blocks(self):
        from tests.test_nexus_oos_promotion_gate import _passing_artifact, _dep
        a = _passing_artifact()
        a["candidate_research"]["inference"]["approved_expectancy"] = _dep(
            0.05, 0.3, 0.04, 0.32, 0.045, 0.31, blocks=(12, 8, 6))
        r = gate.evaluate(a)
        self.assertIn("INSUFFICIENT_RESAMPLING_BLOCKS", r.blockers)
        self.assertFalse(r.promote)

    def test_no_stale_ten_hour_assumption(self):
        self.assertFalse(hasattr(inf, "REPLAY_MAX_HORIZON_MS"))
        rows = self._rows(3 * DAY, days=40)
        sp = inf.purged_split(rows)
        self.assertGreaterEqual(sp["embargo_ms"], 3 * DAY)


class LeakFreeSplitsWithLongAndCensoredTrades(unittest.TestCase):
    def test_censored_and_long_trades_never_cross_partitions(self):
        rows = []
        for i in range(900):
            ts = D0 + i * 2 * 3_600_000
            long_trade = i % 37 == 0
            censored = i % 53 == 0
            rows.append({"ts": ts, "outcome_end_ts": ts + (6 * DAY if long_trade else 3 * 3_600_000),
                         "r": 0.1, "outcome_status": "RIGHT_CENSORED_RESEARCH_LIMIT" if censored else "RESOLVED",
                         "censored": "x" if censored else None})
        sp = inf.purged_split(rows)
        inf.assert_no_leakage(sp)
        self.assertGreaterEqual(sp["embargo_ms"], 6 * DAY)
        for name in ("train", "validation"):
            self.assertFalse(any(r["outcome_status"] != "RESOLVED" for r in sp[name]), name)


class PathBootstrapHasNoAuthority(unittest.TestCase):
    def test_spliced_timeline_is_labelled_and_gate_ignores_it(self):
        from tests.test_nexus_oos_promotion_gate import _passing_artifact
        rows = []
        for d in range(12):
            ts = D0 + d * DAY + 2 * H
            rows.append({"ts": ts, "symbol": ("BTCUSDT", "XRPUSDT", "LINKUSDT")[d % 3],
                         "direction": "LONG", "approved": True, "executable": True,
                         "strategy_score": 70, "score_adjusted": 70, "rr": 3.0, "signal_entry": 100.0,
                         "sl": 99.0, "tp": 103.0, "fill": 100.0,
                         "legs": [(ts + 30 * H, 1.0, 101.0, "NATIVE_TP")],   # crosses the next block
                         "marks": [(ts + k * M15, 100.2) for k in range(1, 120)], "funding": [],
                         "fee_rate": 0.0006, "cost_fraction": 0.0022, "outcome_status": "RESOLVED",
                         "month": "2025-10"})
        pb = pe.path_bootstrap(rows, _manifest(), instruments=INSTR, mmr_proxy=MMR, replicates=100)
        self.assertEqual(pb["authority"], "NONE")
        self.assertEqual(pb["status"], "APPROXIMATE_NON_AUTHORITATIVE")
        self.assertGreater(pb["non_contiguous_block_joins"], 0)
        a = _passing_artifact()
        a["portfolio_replay"]["path_bootstrap"] = {**pb, "net_return_ci": [0.5, 0.9]}
        a["portfolio_replay"]["walk_forward"]["folds_positive"] = 0
        self.assertIn("PORTFOLIO_WALK_FORWARD_NOT_POSITIVE", gate.evaluate(a).blockers)

    def test_walk_forward_uses_real_timeline(self):
        rows = []
        for d in range(0, 120, 3):
            ts = D0 + d * DAY
            rows.append({"ts": ts, "symbol": "BTCUSDT", "direction": "LONG", "approved": True,
                         "executable": True, "strategy_score": 70, "score_adjusted": 70, "rr": 3.0,
                         "signal_entry": 100.0, "sl": 99.0, "tp": 103.0, "fill": 100.0,
                         "legs": [(ts + H, 1.0, 101.0 if d % 2 else 99.5, "X")],
                         "marks": [(ts + k * M15, 100.0) for k in range(1, 4)], "funding": [],
                         "fee_rate": 0.0006, "cost_fraction": 0.0022, "outcome_status": "RESOLVED",
                         "month": "2025-10"})
        wf = pe.walk_forward_folds(rows, _manifest(), instruments=INSTR, mmr_proxy=MMR, folds=4)
        self.assertEqual(wf["status"], "OK")
        self.assertEqual(wf["folds_total"], 4)
        self.assertTrue(wf["folds_overlap_free"])
        # Decisions inside an outcome-completion window or embargo are in no fold.
        self.assertLess(sum(f["candidates"] for f in wf["folds"]), 40)
        self.assertGreater(sum(f["candidates"] for f in wf["folds"]), 20)


class NativeVersusLocalGeometry(unittest.TestCase):
    def test_exchange_native_and_local_position_geometry_differ_by_fill_delta(self):
        sim = xp.simulate_production_exit(
            direction="LONG", bars=_bars([(100.5, 100.6, 100.4, 100.5)] * 3), start_idx=0,
            signal_entry=100.0, signal_sl=99.0, signal_tp=103.0, slippage_rate=0.0)
        native, local = sim["exchange_native_geometry"], sim["local_position_geometry"]
        self.assertEqual(native["sl_initial"], 99.0)
        self.assertEqual(native["tp"], 103.0)
        self.assertAlmostEqual(local["entry"], 100.5)
        self.assertAlmostEqual(local["sl_initial"], 99.5)
        self.assertAlmostEqual(local["tp"], 103.5)


class OutcomeWindows(unittest.TestCase):
    def test_windows_are_explicit_and_decisions_end_before_the_tail(self):
        k15 = _bars([(1, 1, 1, 1)] * (80 + 1000 + 300))
        ts15 = [b["ts"] for b in k15]
        w = replay._windows(k15, ts15, 300)
        self.assertAlmostEqual(w["decision_window_days"], 1000 * 15 / 1440)
        self.assertAlmostEqual(w["outcome_lookforward_days"], 300 * 15 / 1440)
        self.assertAlmostEqual(w["warmup_days"], 80 * 15 / 1440)

    def test_manifest_requires_lookforward_at_least_research_limit(self):
        with self.assertRaisesRegex(rm.ManifestError, "LOOKFORWARD"):
            _manifest(OUTCOME_LOOKFORWARD_BARS=100)


class LivePolicyAttestation(unittest.TestCase):
    def test_line_contains_only_whitelisted_non_secret_values(self):
        secrets = {"KUCOIN_API_KEY": "SECRETKEY123", "KUCOIN_API_SECRET": "SHHH999",
                   "DATABASE_URL": "postgres://u:pw@host/db", "TELEGRAM_TOKEN": "tok-777"}
        with patch.dict(os.environ, secrets):
            line = pa.attestation_line(pa.runtime_policy())
        for v in secrets.values():
            self.assertNotIn(v, line)
        keys = [t.split("=")[0] for t in line.split()[2:]]
        self.assertEqual(tuple(keys), pa.ATTESTED_KEYS)
        self.assertTrue(line.startswith(pa.TAG + " sha256="))

    def test_parse_verifies_hash_and_whitelist(self):
        line = pa.attestation_line(pa.manifest_values(rm.load()))
        self.assertEqual(pa.parse(line)["sha256"], pa.digest(pa.manifest_values(rm.load())))
        with self.assertRaises(ValueError):
            pa.parse(line.replace("LEVERAGE=50", "LEVERAGE=20"))
        with self.assertRaises(ValueError):
            pa.parse(line + " API_KEY=1")

    def test_compare_statuses(self):
        m = rm.load()
        self.assertEqual(pa.compare(m, None)["status"], pa.STATUS_NOT_ATTESTED)
        self.assertEqual(pa.compare(m, pa.attestation_line(pa.manifest_values(m)))["status"], pa.STATUS_MATCH)
        other = dict(pa.manifest_values(m), LEVERAGE=20)
        res = pa.compare(m, "2026 WARNING " + pa.attestation_line(other))
        self.assertEqual(res["status"], pa.STATUS_MISMATCH)
        self.assertEqual(res["mismatches"], {"LEVERAGE": {"replay": "50", "live": "20"}})
        self.assertEqual(pa.compare(m, "garbage")["status"], pa.STATUS_INVALID)

    def test_pending_is_research_only_and_mismatch_blocks_everywhere(self):
        from tests.test_nexus_oos_promotion_gate import _passing_artifact
        a = copy.deepcopy(_passing_artifact())
        a["policy_parity"] = pa.STATUS_NOT_ATTESTED
        self.assertEqual(pa.STATUS_NOT_ATTESTED, gate.LIVE_ATTESTATION_PENDING)
        self.assertTrue(gate.evaluate(a).promote)
        self.assertIn("LIVE_POLICY_NOT_ATTESTED", gate.evaluate_live(
            a, candidate_sha=a["candidate_sha"], protection_readiness="PASS",
            human_authorization="operator").blockers)
        for status in (pa.STATUS_MISMATCH, pa.STATUS_INVALID, None):
            a["policy_parity"] = status
            self.assertIn("LIVE_POLICY_ATTESTATION_MISMATCH_OR_INVALID", gate.evaluate(a).blockers)

    def test_bootstrap_installs_attestation_log(self):
        src = (rm.DEFAULT_PATH.parent.parent / "bot" / "runtime_bootstrap.py").read_text(encoding="utf-8")
        self.assertIn("_policy_attestation.install(_log)", src)


if __name__ == "__main__":
    unittest.main()
