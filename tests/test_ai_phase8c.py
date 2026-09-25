"""Phase 8C: predeclared nested challenger research (research branch only)."""
import copy
import gzip
import json
import os
import random
import tempfile
import unittest
import unittest.mock

from bot import nexus_oos_inference as inf
from bot.ai import challenger_research as cr
from bot.ai import features as fx
from bot.ai.decision import ABSTAIN_ALL
from tests.test_ai_decision_authority import H1, T0

SYMS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT")
SCEN = ("current", "fees_plus_25pct", "fees_plus_50pct", "slippage_x1_5", "slippage_x2", "combined_adverse")


def rows(n=1200, seed=7, signal=1.2, mean=0.0):
    rng = random.Random(seed)
    out, d = [], len(fx.MODEL_FEATURES)
    for i in range(n):
        f = [rng.gauss(0, 1) for _ in range(d)]
        f[fx.MODEL_FEATURES.index("cost_fraction")] = 0.002
        f[fx.MODEL_FEATURES.index("stop_distance_pct")] = 0.02
        f[fx.MODEL_FEATURES.index("nexus_confidence")] = 60.0
        ts = T0 + i * 3 * H1
        r = mean + signal * f[0] + rng.gauss(0, 0.6)
        out.append({"ts": ts, "outcome_end_ts": ts + H1, "ai_hook_eligible": True, "approved": i % 3 == 0,
                    "outcome_status": "RESOLVED", "r": r, "gross_r": r + 0.1, "ai_features": f,
                    "ai_feature_hash": f"h{i}", "direction": "LONG" if i % 2 else "SHORT",
                    "symbol": SYMS[i % len(SYMS)], "ai_regime": ["TREND_UP", "RANGE", "CHOP"][i % 3],
                    "month": f"2026-{1 + (i * 3 * H1 // (30 * 86_400_000)) % 12:02d}",
                    "cost_r": {k: r - 0.02 * j for j, k in enumerate(SCEN)}})
    return out


_CACHE = {}


def research(key):
    if key not in _CACHE:
        rs = rows(n=2400) if key == "signal" else rows(signal=0.0, mean=-0.35)
        _CACHE[key] = (rs, cr.run(rs, required_ms=inf.DAY_MS))
    return _CACHE[key]


class SearchSpec(unittest.TestCase):
    def test_search_is_predeclared_small_and_hashed(self):
        self.assertEqual(len(list(cr.candidate_specs())), 32)
        self.assertEqual(len(cr.SEARCH_SPEC["selection_criteria"]), 2)
        self.assertEqual(cr.SEARCH_SPEC["fail_closed_rules"]["min_validation_trades"], 30)
        self.assertEqual(cr.SEARCH_SPEC["fail_closed_rules"]["min_group_trades"], 30)
        self.assertEqual(len(cr.SEARCH_SPEC_SHA256), 64)
        self.assertEqual(cr.EVIDENCE_LABEL, "PREVIOUSLY_INSPECTED_RESEARCH_SUPPORT")


class NoEdge(unittest.TestCase):
    def test_negative_noise_gives_no_valid_challenger_and_refuses_freeze(self):
        rs, res = research("noise")
        self.assertEqual(res["result"], "NO_VALID_CHALLENGER")
        self.assertFalse(res["support"]["all_pass"])
        self.assertEqual(res["candidates_evaluated"], 64)
        self.assertEqual(len(res["ledger"]), 128)                  # 64 candidates x 2 outer steps
        with self.assertRaises(ValueError):
            cr.freeze(rs, res, training_code_sha="c" * 40, created_at="2026-09-25T00:00:00Z")


class WithEdge(unittest.TestCase):
    def test_planted_signal_is_found_frozen_and_loadable(self):
        rs, res = research("signal")
        self.assertEqual(res["status"], "OK")
        self.assertEqual(res["result"], "CANDIDATE_SUPPORTED", res["support"]["failures"])
        ch = cr.freeze(rs, res, training_code_sha="c" * 40, created_at="2026-09-25T00:00:00Z")
        self.assertEqual(ch["lifecycle_state"], "SHADOW_CHALLENGER")
        self.assertNotEqual(ch["decision_policy_sha256"], ABSTAIN_ALL.sha256)
        from bot.ai import bundle_export as bx
        tmp = tempfile.mkdtemp()
        art = {"candidate_sha": "c" * 40, "candidate_research": {"ai_meta_model": {
            "shadow_challenger": ch, "dataset_manifest": cr.tr.dataset_manifest(rs)}}}
        bx.export(art, tmp, candidate_sha="c" * 40)
        v = bx.verify(tmp)
        self.assertTrue(v["verified"])
        self.assertEqual(v["lifecycle_state"], "SHADOW_CHALLENGER")
        self.assertEqual(ch["bundle"]["selection_evidence"]["search_spec_sha256"], cr.SEARCH_SPEC_SHA256)

    def test_outer_evaluation_fold_never_selects(self):
        rs, res = research("signal")
        folds = res["_folds"]
        f4 = {id(r) for r in folds[3]}
        shuffled = copy.deepcopy(rs)
        rng = random.Random(1)
        idx = [i for i, r in enumerate(rs) if id(r) in f4]
        vals = [shuffled[i]["r"] for i in idx]
        rng.shuffle(vals)
        for i, v in zip(idx, vals):
            shuffled[i]["r"] = v
        res2 = cr.run(shuffled, required_ms=inf.DAY_MS)
        self.assertEqual(res2["steps"][1]["selected"], res["steps"][1]["selected"])   # F4 only evaluates

    def test_ledger_marks_posthoc_outer_results(self):
        _, res = research("signal")
        e = res["ledger"][0]
        self.assertIn("outer_posthoc_not_used_for_selection", e)
        self.assertIn("NOT a result", res["max_outer_posthoc_note"])


class HookRowDump(unittest.TestCase):
    def test_replay_dump_preserves_the_exact_training_dataset(self):
        from bot import nexus_oos_real_replay as rp
        p = os.path.join(tempfile.mkdtemp(), "d", "rows.jsonl.gz")
        rs = rows(n=300)
        with unittest.mock.patch.dict(os.environ, {"OOS_HOOK_ROWS_OUT": p}):
            rp._dump_hook_rows(rs, inf.DAY_MS)
        with gzip.open(p, "rt") as fh:
            h = json.loads(fh.readline())["header"]
            back = [json.loads(line) for line in fh]
        self.assertEqual(h["required_ms"], inf.DAY_MS)
        self.assertEqual(cr.tr.dataset_manifest(back)["sha256"], cr.tr.dataset_manifest(rs)["sha256"])
        self.assertEqual(h["dataset_manifest"]["sha256"], cr.tr.dataset_manifest(rs)["sha256"])


class Cli(unittest.TestCase):
    def test_rows_file_roundtrip_and_outputs(self):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "rows.jsonl.gz")
        with gzip.open(p, "wt") as fh:
            fh.write(json.dumps({"header": {"required_ms": inf.DAY_MS}}) + "\n")
            for r in rows(signal=0.0, mean=-0.35):
                fh.write(json.dumps(r) + "\n")
        out = os.path.join(tmp, "out")
        self.assertEqual(cr.main(["--rows", p, "--out", out, "--candidate-sha", "c" * 40,
                                  "--created-at", "2026-09-25T00:00:00Z"]), 0)
        s = json.load(open(os.path.join(out, "challenger_summary.json")))
        self.assertEqual(s["result"], "NO_VALID_CHALLENGER")
        self.assertNotIn("frozen", s)
        self.assertEqual(s["evidence_label"], "PREVIOUSLY_INSPECTED_RESEARCH_SUPPORT")
        self.assertFalse(os.path.exists(os.path.join(out, "challenger_bundle")))


if __name__ == "__main__":
    unittest.main()
