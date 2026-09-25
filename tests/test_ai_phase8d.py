"""Phase 8D: SIGNAL_DISCOVERY_SPEC_V1 (research branch only)."""
import copy
import gzip
import json
import os
import random
import tempfile
import unittest

from bot import nexus_oos_inference as inf
from bot.ai import features as fx
from bot.ai import signal_discovery as sd
from bot.ai.decision import ABSTAIN_ALL
from tests.test_ai_phase8c import rows as base_rows

CREATED = "2026-09-25T00:00:00Z"
SHA = "c" * 40


def rows(n=2400, seed=7, signal=1.2, mean=0.0):
    """Phase-8C fixture + the replay's forward path / geometry fields."""
    rng = random.Random(seed + 1)
    out = base_rows(n=n, seed=seed, signal=signal, mean=mean)
    for r in out:
        long_ = r["direction"] == "LONG"
        r.update({"entry_fill": 100.0, "stop": 98.0 if long_ else 102.0, "rr": 2.0, "cost_fraction": 0.002,
                  "fees_r": -0.05, "slippage_r": -0.02, "funding_r": 0.0, "exit_reason": "SL" if r["r"] < 0 else "TP",
                  "nexus_confidence": 60.0, "outcome_horizon_ms": 3_600_000})
        drift = (1 if long_ else -1) * 0.25 * r["r"] / 16
        px, fwd = 100.0, []
        for k in range(17):
            o = px
            px = px + drift + rng.gauss(0, 0.3)
            fwd.append([r["ts"] + k * sd.BAR_MS, o, max(o, px) + 0.1, min(o, px) - 0.1, px])
        r["_fwd"] = fwd
        r["rf"] = {k: rng.gauss(0, 1) for k in sd.rfx.RESEARCH_FEATURES}
    return out


_CACHE = {}


def research(key):
    if key not in _CACHE:
        rs = {"signal": lambda: rows(),
              "noise": lambda: rows(n=1200, signal=0.0, mean=0.0),
              "negative": lambda: rows(n=1200, signal=0.0, mean=-0.35)}[key]()
        _CACHE[key] = (rs, sd.run(rs, required_ms=inf.DAY_MS))
    return _CACHE[key]


class Spec(unittest.TestCase):
    def test_spec_predeclared_bounded_hashed(self):
        self.assertEqual(len(list(sd.promotable_specs())), 72)
        self.assertEqual(len(sd.SPEC_SHA256), 64)
        self.assertEqual(sd.SPEC["fail_closed_rules"]["min_group_trades"], 30)
        self.assertEqual(sd.SPEC["supporting_gate"]["min_pooled_trades"], 60)
        self.assertEqual(sd.EVIDENCE_LABEL, "PREVIOUSLY_INSPECTED_RESEARCH_SUPPORT")


class NoEdge(unittest.TestCase):
    def test_01_noise_gives_no_valid_challenger(self):
        _, res = research("noise")
        self.assertEqual(res["result"], "NO_VALID_CHALLENGER")
        self.assertFalse(res["gate"]["all_pass"])

    def test_02_negative_expectancy_gives_no_valid_challenger(self):
        _, res = research("negative")
        self.assertEqual(res["result"], "NO_VALID_CHALLENGER")
        self.assertEqual(len(res["ledger"]), 2 * res["candidates_evaluated"])
        self.assertIn("SIGNAL", res["attribution"]["verdict"])

    def test_10_freeze_impossible_after_failed_gate(self):
        rs, res = research("negative")
        with self.assertRaises(ValueError):
            sd.freeze(rs, res, training_code_sha=SHA, created_at=CREATED)


class Planted(unittest.TestCase):
    def test_03_planted_signal_supported_and_frozen(self):
        rs, res = research("signal")
        self.assertEqual(res["result"], "CANDIDATE_SUPPORTED", res["gate"]["failures"])
        ch = sd.freeze(rs, res, training_code_sha=SHA, created_at=CREATED)
        self.assertEqual(ch["lifecycle_state"], "SHADOW_CHALLENGER")
        self.assertNotEqual(ch["decision_policy_sha256"], ABSTAIN_ALL.sha256)
        ev = ch["bundle"]["selection_evidence"]
        for k in ("search_spec_sha256", "target_spec_sha256", "cost_policy_sha256", "exit_policy_sha256"):
            self.assertEqual(len(ev[k]), 64)

    def test_04_outer_relabel_does_not_change_selection(self):
        rs, res = research("signal")
        f4 = {id(r) for r in res["_folds"][3]}
        mut = copy.deepcopy(rs)
        idx = [i for i, r in enumerate(rs) if id(r) in f4]
        vals = [mut[i]["r"] for i in idx]
        random.Random(1).shuffle(vals)
        for i, v in zip(idx, vals):
            mut[i]["r"] = v
            mut[i]["gross_r"] = v + 0.1
        res2 = sd.run(mut, required_ms=inf.DAY_MS)
        self.assertEqual(res2["steps"][1]["selected"], res["steps"][1]["selected"])

    def test_05_future_mutation_does_not_affect_earlier_selection(self):
        rs, res = research("signal")
        later = {id(r) for f in res["_folds"][2:] for r in f}
        mut = copy.deepcopy(rs)
        for i, r in enumerate(rs):
            if id(r) in later:
                mut[i]["r"] = -mut[i]["r"]
                mut[i]["gross_r"] = mut[i]["r"] + 0.1
        res2 = sd.run(mut, required_ms=inf.DAY_MS)
        self.assertEqual(res2["steps"][0]["selected"], res["steps"][0]["selected"])   # step 1 uses F1/F2 only

    def test_11_deterministic_export_load_inference(self):
        from bot.ai import bundle_export as bx
        rs, res = research("signal")
        ch1 = sd.freeze(rs, res, training_code_sha=SHA, created_at=CREATED)
        ch2 = sd.freeze(rs, res, training_code_sha=SHA, created_at=CREATED)
        self.assertEqual(ch1["bundle_sha256"], ch2["bundle_sha256"])
        tmp = tempfile.mkdtemp()
        art = {"candidate_sha": SHA, "candidate_research": {"ai_meta_model": {
            "shadow_challenger": ch1, "dataset_manifest": sd.tr.dataset_manifest(rs)}}}
        bx.export(art, tmp, candidate_sha=SHA)
        v1, v2 = bx.verify(tmp), bx.verify(tmp)
        self.assertTrue(v1["verified"])
        self.assertEqual(v1["lifecycle_state"], "SHADOW_CHALLENGER")
        self.assertEqual(v1["deterministic_inference"], v2["deterministic_inference"])

    def test_12_dataset_hash_preserved(self):
        rs, res = research("signal")
        self.assertEqual(res["dataset_manifest"]["sha256"], sd.tr.dataset_manifest(copy.deepcopy(rs))["sha256"])
        ch = sd.freeze(rs, res, training_code_sha=SHA, created_at=CREATED)
        self.assertEqual(ch["bundle"]["training_dataset_manifest_sha256"], res["dataset_manifest"]["sha256"])

    def test_13_runtime_feature_parity(self):
        rs, res = research("signal")
        ch = sd.freeze(rs, res, training_code_sha=SHA, created_at=CREATED)
        self.assertEqual(ch["feature_schema_sha256"], fx.schema_hash())
        self.assertEqual(list(ch["classifier_artifact"]["feature_names"]), list(fx.MODEL_FEATURES))
        par = ch["bundle"]["selection_evidence"]["runtime_parity"]
        self.assertTrue(par["parity"])
        self.assertFalse(par["research_features_used"])
        fd = res["feature_diagnostics"]["research_features"]
        self.assertTrue(all(not v["promotable"] and not v["runtime_parity"] for v in fd.values()))


class GateUnits(unittest.TestCase):
    def _steps(self, n=200, r=0.4, sym=None, month=None, cost=None, prob=False, calib_ok=True):
        rs, rng = [], random.Random(3)
        for i in range(n):
            ts = 1_760_000_000_000 + i * 6 * 3_600_000
            v = r + rng.gauss(0, 0.3)
            rs.append({"ts": ts, "r": v, "symbol": sym or ("BTCUSDT", "ETHUSDT", "XRPUSDT")[i % 3],
                       "month": month or f"2026-0{1 + i % 4}", "ai_regime": ("TREND_UP", "RANGE", "TREND_DOWN")[i % 3],
                       "direction": "LONG", "cost_r": {k: (cost if cost is not None else v)
                                                       for k in sd.REQUIRED_COST_SCENARIOS + ("current",)}})
        base = [dict(x, r=rng.gauss(-0.5, 0.5)) for x in rs]
        pol = {"allowed_directions": ["LONG"], "supported_regimes": ["TREND_UP", "RANGE", "TREND_DOWN"]}
        sel = {"candidate_id": "x-MAX_MEAN_R", "policy": pol, "policy_sha256": "p" * 64}
        cr = {"beats_base_rate": calib_ok, "ece": 0.02 if calib_ok else 0.3}
        steps = [{"selected": sel, "test": rs[:n // 2] + base[:n // 2], "approved": rs[:n // 2],
                  "test_calibration": cr, "probability_authorizes": prob},
                 {"selected": sel, "test": rs[n // 2:] + base[n // 2:], "approved": rs[n // 2:],
                  "test_calibration": cr, "probability_authorizes": prob}]
        ledger = [{"step": 1, "candidate_id": "x-MAX_MEAN_R", "abstain_all": False, "validation": {"score": 0.2}}]
        return steps, ledger

    def test_gate_passes_clean_synthetic_edge(self):
        g = sd.gate(*self._steps(), inf.DAY_MS)
        self.assertTrue(g["all_pass"], g["failures"])

    def test_07_calibration_failure_removes_probability_authority(self):
        pol, _, reason = sd.select_policy([], [], [], "MAX_MEAN_R", authority="PROB_AND_NET", calibration_ok=False)
        self.assertEqual(pol.sha256, ABSTAIN_ALL.sha256)
        self.assertEqual(reason, "CALIBRATION_DOES_NOT_BEAT_BASE_RATE")
        g = sd.gate(*self._steps(prob=True, calib_ok=False), inf.DAY_MS)
        self.assertIn("CALIBRATION_FAILURE_WITH_PROBABILITY_AUTHORITY", g["failures"])
        g2 = sd.gate(*self._steps(prob=False, calib_ok=False), inf.DAY_MS)   # net-edge authority tested separately
        self.assertNotIn("CALIBRATION_FAILURE_WITH_PROBABILITY_AUTHORITY", g2["failures"])

    def test_08_cost_stress_vetoes(self):
        g = sd.gate(*self._steps(cost=-0.01), inf.DAY_MS)
        self.assertIn("COST_STRESS_FAILS_COMBINED_ADVERSE", g["failures"])
        self.assertFalse(g["all_pass"])

    def test_09_concentration_vetoes(self):
        g = sd.gate(*self._steps(sym="BTCUSDT"), inf.DAY_MS)
        self.assertIn("SINGLE_SYMBOL_DOMINATES", g["failures"])
        g = sd.gate(*self._steps(month="2026-03"), inf.DAY_MS)
        self.assertIn("SINGLE_MONTH_DOMINATES", g["failures"])

    def test_06_insufficient_group_disables(self):
        from bot.ai import training as tr
        val = [{"ts": i, "r": 1.0, "direction": "LONG", "ai_regime": "TREND_UP",
                "ai_features": [0.0] * len(fx.MODEL_FEATURES)} for i in range(tr.MIN_GROUP_TRADES - 1)]
        self.assertNotEqual(tr._status(val), tr.ENABLED)
        pol, _, reason = sd.select_policy(val, [0.9] * len(val), [5.0] * len(val), "MAX_MEAN_R",
                                          authority="NET_ONLY", calibration_ok=True)
        self.assertEqual(pol.sha256, ABSTAIN_ALL.sha256)


class ForwardEvidence(unittest.TestCase):
    def test_14_no_forward_phase8_evidence_used(self):
        rs = rows(n=300, signal=0.0)
        rs[-1] = dict(rs[-1], ts=sd.FORWARD_EVIDENCE_CUTOFF_MS, outcome_end_ts=sd.FORWARD_EVIDENCE_CUTOFF_MS + 1)
        with self.assertRaises(sd.ForwardEvidenceRefused):
            sd.run(rs, required_ms=inf.DAY_MS)


class Cli(unittest.TestCase):
    def test_cli_writes_all_artifacts_and_no_bundle_on_failure(self):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "rows.jsonl.gz")
        with gzip.open(p, "wt") as fh:
            fh.write(json.dumps({"header": {"required_ms": inf.DAY_MS}}) + "\n")
            for r in rows(n=1200, signal=0.0, mean=-0.35):
                fh.write(json.dumps(r) + "\n")
        out = os.path.join(tmp, "out")
        self.assertEqual(sd.main(["--rows", p, "--out", out, "--candidate-sha", SHA, "--created-at", CREATED]), 0)
        for f in ("signal_diagnostics.json", "loss_attribution.json", "horizon_analysis.json",
                  "feature_diagnostics.json", "exit_research.json", "candidate_ledger.json.gz",
                  "challenger_summary.json", "search_spec.json"):
            self.assertTrue(os.path.exists(os.path.join(out, f)), f)
        with open(os.path.join(out, "challenger_summary.json")) as fh:
            s = json.load(fh)
        self.assertEqual(s["result"], "NO_VALID_CHALLENGER")
        self.assertNotIn("frozen", s)
        self.assertFalse(os.path.exists(os.path.join(out, "challenger_bundle")))


if __name__ == "__main__":
    unittest.main()
