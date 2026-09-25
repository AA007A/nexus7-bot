"""Phase 8G-B: incremental alpha (funding + basis + xsec) - research branch only."""
import copy
import math
import os
import random
import tempfile
import unittest

import numpy as np

from bot.ai import exo_features as ex
from bot.ai import incremental_alpha as ia
from bot.ai import xsec_features as xf

H1, DAY = ia.H1, ia.DAY
SYMS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT")


# ── synthetic research dataset (arrays) ────────────────────────────────────
def synth(days=500, planted=None, strength=0.6, base=0.05, seed=1, syms=SYMS):
    rng = np.random.default_rng(seed)
    T0 = ia.WINDOW_START_MS - ia.WINDOW_START_MS % (4 * H1) + 4 * H1
    Ts = T0 + np.arange(days * 6) * 4 * H1
    T = np.repeat(Ts, len(syms))
    sym = np.tile(np.array(syms), len(Ts))
    n = len(T)
    ds = {"T": T.astype(np.int64), "sym": sym, "sf": np.full(n, 0.02),
          "regime": np.array([("TREND_UP", "RANGE", "MIXED")[i % 3] for i in range(n)]),
          "P_L": rng.normal(size=(n, len(ia.PRICE_KEYS))), "X": rng.normal(size=(n, len(ia.XSEC_KEYS))),
          "F": rng.normal(size=(n, len(ex.F_KEYS))), "B": rng.normal(size=(n, len(ex.B_KEYS)))}
    ds["P_S"] = -ds["P_L"]
    sig = {"F": ds["F"][:, 0], "B": ds["B"][:, 0], "P": ds["P_L"][:, 0], "X": ds["X"][:, 0], None: np.zeros(n)}[planted]
    ds["tgt"] = {}
    for side, sv in (("L", 1.0), ("S", -1.0)):
        for H in ia.HORIZONS:
            gross = base + strength * sv * sig + rng.normal(0, 0.8, n) + 0.25
            fee, slip, fund = np.full(n, 0.06), np.full(n, 0.08), np.full(n, 0.01)
            ds["tgt"][(side, H)] = {"gross": gross, "fee": fee, "slip": slip, "fund": fund,
                                    "exit_ts": T + H * H1, "n_settle": np.full(n, H / 8),
                                    "net": gross - fee - slip - fund - ia.BUFFER_R}
    ds["month"] = np.array([ia._month(t) for t in T])
    ds["missing"], ds["effective"] = {}, {}
    return ds


_CACHE = {}


def run(key):
    if key not in _CACHE:
        kw = {"funding": dict(planted="F"), "noise": dict(planted=None, base=-0.3), "price": dict(planted="P"),
              "short": dict(planted="F", days=200)}[key]
        ds = synth(**kw)
        _CACHE[key] = (ds, ia.run_experiment(ds))
    return _CACHE[key]


# ── raw-data fixtures for feature semantics ────────────────────────────────
def settlements(n=80, t0=1_750_000_000_000 // (8 * H1) * (8 * H1), step=8 * H1, seed=2):
    rng = random.Random(seed)
    return [(t0 + k * step, rng.gauss(1e-4, 1e-4)) for k in range(n)]


class FundingSemantics(unittest.TestCase):
    def test_01_funding_before_settlement_cannot_leak(self):
        s = settlements()
        T = s[70][0] - 1                                   # 1 ms before settlement 70
        a = ex.funding_at(s, T, 0.01)
        mut = copy.deepcopy(s)
        mut[70] = (mut[70][0], 0.05)                       # change a not-yet-observable settlement
        self.assertEqual(a, ex.funding_at(mut, T, 0.01))
        self.assertEqual(a["f_last"], s[69][1])
        r = ex.funding_series(np.array([t for t, _ in s]), np.array([v for _, v in s]), np.array([T]), np.array([0.01]))
        self.assertEqual(r["f_last"][0], s[69][1])

    def test_02_irregular_intervals_handled_by_settlement_index(self):
        s = settlements(n=70)
        irregular = [(t if k < 40 else s[39][0] + (k - 39) * 4 * H1, v) for k, (t, v) in enumerate(s)]
        T = irregular[-1][0] + H1
        a = ex.funding_at(irregular, T, 0.0)
        self.assertEqual(a["f_last"], irregular[-1][1])
        self.assertAlmostEqual(a["f_age_h"], 1.0)
        r = ex.funding_series(np.array([t for t, _ in irregular]), np.array([v for _, v in irregular]),
                              np.array([T]), np.array([0.0]))
        for k in ex.F_KEYS:
            self.assertAlmostEqual(float(r[k][0]), a[k], places=12)

    def test_03_stale_plateau_preserved_and_flagged(self):
        s = settlements()
        s[-3:] = [(t, 1e-4) for t, _ in s[-3:]]
        a = ex.funding_at(s, s[-1][0], 0.0)
        self.assertEqual(a["f_stale"], 1.0)
        self.assertEqual(a["f_last"], 1e-4)
        self.assertEqual(a["f_z21"], a["f_z21"])            # finite, not dropped

    def test_04_missing_funding_is_not_zero(self):
        s = settlements()
        T = s[-1][0] + 20 * H1                             # last settlement older than 16h => feed gap
        a = ex.funding_at(s, T, 0.0)
        self.assertTrue(all(math.isnan(v) for v in a.values()))
        s2 = settlements()
        s2[-5] = (s2[-5][0], None)
        self.assertTrue(math.isnan(ex.funding_at(s2, s2[-1][0], 0.0)["f_last"]))


class BasisSemantics(unittest.TestCase):
    def _bars(self, n=200, t0=1_750_000_000_000 // H1 * H1):
        rng = random.Random(4)
        perp = {t0 + k * H1: 100 + rng.random() for k in range(n)}
        index = {t: p * (1 - rng.gauss(0, 1e-3)) for t, p in perp.items()}
        return perp, index, t0

    def test_05_basis_uses_matched_completed_bars(self):
        perp, index, t0 = self._bars()
        T = t0 + 190 * H1
        a = ex.basis_at(perp, index, T, 0.0)
        last_bar = T - H1
        self.assertAlmostEqual(a["b_now"], perp[last_bar] / index[last_bar] - 1)
        mut = dict(index)
        mut[T] = 1.0                                       # the forming bar at T must not matter
        self.assertEqual(a, ex.basis_at(perp, mut, T, 0.0))

    def test_06_missing_index_candle_marks_missing(self):
        perp, index, t0 = self._bars()
        T = t0 + 190 * H1
        del index[T - 5 * H1]
        a = ex.basis_at(perp, index, T, 0.0)
        self.assertTrue(all(math.isnan(v) for v in a.values()))
        hg = np.array(sorted(perp), np.int64)
        r = ex.basis_series(hg, ex.basis_hourly(hg, perp, index), np.array([T]), np.array([0.0]))
        self.assertTrue(np.isnan(r["b_now"][0]))


class XsecLeak(unittest.TestCase):
    def test_07_future_symbol_cannot_leak_backward(self):
        rng = random.Random(1)
        t0 = 1_750_000_000_000 // H1 * H1
        u = {}
        for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
            p, bars = 100.0, []
            for i in range(24 * 45):
                o, p = p, p * math.exp(rng.gauss(0, 0.006))
                bars.append({"ts": t0 + i * H1, "o": o, "h": max(o, p), "l": min(o, p), "c": p, "v": 1000.0})
            u[s] = bars
        T = xf.grid(u)
        cut = T[len(T) // 2]
        a = xf.matrix(u, T[T <= cut])
        u2 = copy.deepcopy(u)
        u2["NEWUSDT"] = [dict(b) for b in u["SOLUSDT"] if b["ts"] + H1 > cut]
        b = xf.matrix(u2, T[T <= cut])
        for f in xf.MARKET:
            np.testing.assert_array_equal(a["market"][f], b["market"][f])


class Matching(unittest.TestCase):
    def test_08_identical_rows_across_ablations(self):
        ds, res = run("funding")
        idx = np.arange(100)
        rows = {k: ia.design(ds, k, "L", idx).shape[0] for k in ia.SETS}
        self.assertEqual(len(set(rows.values())), 1)
        led = res["ledger"]
        counts = {}
        for e in led:
            counts.setdefault((e["step"], e["cfg"]), set()).add(e["set"])
        self.assertTrue(all(len(v) == len(ia.SETS) for v in counts.values()))

    def test_09_identical_model_capacity(self):
        cfgs = list(ia.configs())
        self.assertEqual(len(cfgs), ia.N_CONFIGS)
        self.assertEqual(ia.SEARCH["promotable_configurations"], 8 * ia.N_CONFIGS)
        self.assertLess(ia.SEARCH["total_configurations"], 500)
        _, res = run("funding")
        per = {}
        for e in res["ledger"]:
            per.setdefault(e["set"], set()).add(e["cfg"])
        self.assertEqual(len({frozenset(v) for v in per.values()}), 1)


class Selection(unittest.TestCase):
    def test_10_outer_outcomes_cannot_alter_inner_selection(self):
        ds, res = run("funding")
        f = res["_fold"]
        mut = copy.deepcopy(ds)
        m = f == 3
        for key in mut["tgt"]:
            mut["tgt"][key]["net"][m] = -mut["tgt"][key]["net"][m]
            mut["tgt"][key]["gross"][m] = -mut["tgt"][key]["gross"][m]
        res2 = ia.run_experiment(mut)
        self.assertEqual(res2["primary_selection"][1]["cfg"], res["primary_selection"][1]["cfg"])
        self.assertEqual(res2["primary_selection"][1]["set"], res["primary_selection"][1]["set"])

    def test_11_target_mutation_in_outer_fold_cannot_change_earlier_selection(self):
        ds, res = run("funding")
        f = res["_fold"]
        mut = copy.deepcopy(ds)
        m = np.isin(f, [2, 3])
        for key in mut["tgt"]:
            mut["tgt"][key]["net"][m] = 0.0
        res2 = ia.run_experiment(mut)
        self.assertEqual(res2["primary_selection"][0], res["primary_selection"][0])

    def test_16_positive_matched_uplift_fixture_passes(self):
        _, res = run("funding")
        self.assertEqual(res["result"], "CANDIDATE_SUPPORTED", res["gate"]["failures"])
        self.assertIn(res["primary_selection"][1]["set"], ("C", "E", "F", "H"))

    def test_15_positive_model_without_improvement_is_rejected(self):
        _, res = run("price")
        self.assertNotEqual(res["result"], "CANDIDATE_SUPPORTED")
        self.assertGreater(res["families"]["per_set_outer"]["A"].get("mean_r", -1), 0)   # price-only itself is positive

    def test_17_noise_returns_no_incremental_alpha(self):
        _, res = run("noise")
        self.assertEqual(res["result"], "NO_INCREMENTAL_ALPHA", res.get("classification_reason"))

    def test_18_insufficient_aligned_history(self):
        _, res = run("short")
        self.assertEqual(res["result"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(res["status"], "INSUFFICIENT_ALIGNED_HISTORY")


class Costs(unittest.TestCase):
    def test_12_realized_funding_uses_actual_held_settlements(self):
        n = 60
        ts = 1_750_000_000_000 // (8 * H1) * (8 * H1) + np.arange(n) * H1
        o = np.full(n, 100.0)
        arr = (o, o + 0.1, o - 0.1, o)
        st = np.array([ts[0] + 8 * H1, ts[0] + 16 * H1, ts[0] + 24 * H1], np.int64)
        rates = np.array([1e-4, 2e-4, -5e-4])
        cum = np.concatenate([[0.0], np.cumsum(rates)])
        r = ia.simulate_targets(arr, ts, np.array([0]), np.array([1.0]), np.array([100.0]), np.array([2.0]),
                                np.array([0.02]), st, cum, "BTCUSDT")
        self.assertAlmostEqual(r[12]["fund"][0], 1e-4 / 0.02)                 # only the 8h settlement held
        self.assertAlmostEqual(r[24]["fund"][0], (1e-4 + 2e-4 - 5e-4) / 0.02)  # exit at +24h includes the 24h one
        rs = ia.simulate_targets(arr, ts, np.array([0]), np.array([-1.0]), np.array([100.0]), np.array([2.0]),
                                 np.array([0.02]), st, cum, "BTCUSDT")
        self.assertAlmostEqual(rs[12]["fund"][0], -1e-4 / 0.02)               # SHORT receives positive funding

    def test_13_no_funding_double_count(self):
        ds = synth(days=40, planted="F")
        before = copy.deepcopy(ds["tgt"])
        ds["F"][:] = 99.0                                   # predictive input changes, realized cost must not
        for k in before:
            np.testing.assert_array_equal(before[k]["net"], ds["tgt"][k]["net"])
        t = ds["tgt"][("L", 24)]
        np.testing.assert_allclose(t["net"], t["gross"] - t["fee"] - t["slip"] - t["fund"] - ia.BUFFER_R)

    def test_19_cost_stress_veto(self):
        ds = synth(planted="F")
        for key in ds["tgt"]:
            ds["tgt"][key]["fee"] = ds["tgt"][key]["fee"] * 0 + 1.5        # huge fees: stress fails
            t = ds["tgt"][key]
            t["net"] = t["gross"] - 0.06 - t["slip"] - t["fund"] - ia.BUFFER_R
        res = ia.run_experiment(ds)
        self.assertNotEqual(res["result"], "CANDIDATE_SUPPORTED")
        if "ABSTAIN_IN_SOME_STEP" not in res["gate"]["failures"]:
            self.assertIn("COST_STRESS_FAILS_FEES_X1_5", res["gate"]["failures"])


class Gates(unittest.TestCase):
    def test_14_calibration_failure_disables_probability(self):
        ds, res = run("noise")
        logi = [e for e in res["ledger"] if "LOGISTIC" in e["cfg"] and not e["calibration_ok"]]
        self.assertTrue(all(not e["eligible"] for e in logi))

    def test_20_concentration_veto(self):
        ds = synth(planted="F", syms=("BTCUSDT",))
        res = ia.run_experiment(ds)
        self.assertNotEqual(res["result"], "CANDIDATE_SUPPORTED")
        if "ABSTAIN_IN_SOME_STEP" not in res["gate"]["failures"]:
            self.assertIn("SINGLE_SYMBOL_DOMINATES", res["gate"]["failures"])

    def test_23_failed_gate_cannot_freeze(self):
        ds, res = run("noise")
        with self.assertRaises(ValueError):
            ia.freeze(res, ds, identities={}, code_sha="c" * 40, created_at="x")


class Integrity(unittest.TestCase):
    def test_21_runtime_feature_parity(self):
        s = settlements(n=120)
        T = np.array([s[k][0] + H1 * (k % 5) for k in range(63, 120, 3)])
        ret = np.array([0.01 * ((-1) ** k) for k in range(len(T))])
        r = ex.funding_series(np.array([t for t, _ in s]), np.array([v for _, v in s]), T, ret)
        rep = ex.parity(r, [(q, ex.funding_at(s, int(t), float(ret[q]))) for q, t in enumerate(T)], ex.F_KEYS)
        self.assertTrue(rep["parity"], rep["examples"])
        bad = ex.parity(r, [(q, dict(ex.funding_at(s, int(t), float(ret[q])), f_z21=0.123)) for q, t in enumerate(T)],
                        ex.F_KEYS)
        self.assertFalse(bad["parity"])

    def test_22_deterministic_dataset_hash(self):
        a, b = synth(days=30), synth(days=30)
        self.assertEqual(ia.dataset_sha256(a), ia.dataset_sha256(b))
        b["F"][0, 0] += 1e-6
        self.assertNotEqual(ia.dataset_sha256(a), ia.dataset_sha256(b))

    def test_24_deterministic_bundle_roundtrip(self):
        ds, res = run("funding")
        self.assertEqual(res["result"], "CANDIDATE_SUPPORTED")
        b1 = ia.freeze(res, ds, identities={"training_dataset_sha256": "d" * 64}, code_sha="c" * 40, created_at="x")
        b2 = ia.freeze(res, ds, identities={"training_dataset_sha256": "d" * 64}, code_sha="c" * 40, created_at="x")
        self.assertEqual(b1["manifest"]["bundle_sha256"], b2["manifest"]["bundle_sha256"])
        self.assertEqual(b1["manifest"]["schema"], "INCREMENTAL_ALPHA_BUNDLE_V1")
        tmp = tempfile.mkdtemp()
        ia.export(b1, tmp)
        v1, v2 = ia.verify(tmp), ia.verify(tmp)
        self.assertTrue(v1["verified"], v1["failures"])
        self.assertEqual(v1, v2)
        p = os.path.join(tmp, "policy.json")
        with open(p) as fh:
            txt = fh.read().replace('"order_authority":false', '"order_authority":true')
        with open(p, "w") as fh:
            fh.write(txt)
        self.assertFalse(ia.verify(tmp)["verified"])


class Spec(unittest.TestCase):
    def test_spec_hashed_and_bounded(self):
        self.assertEqual(len(ia.SPEC_SHA256), 64)
        self.assertEqual(ia.SEARCH["promotable_configurations"], 216)
        self.assertEqual(ia.EXPECTED["alpha_data_contract_sha256"],
                         "e21132a89d9739e6acd66920a8ec595975ad295372f9b7d75a7f7fc4990ab235")


if __name__ == "__main__":
    unittest.main()
