"""Phase 8F: LONG_HORIZON_ARCHITECTURE_SPEC_V1 (research branch only)."""
import asyncio
import copy
import datetime as dt
import gzip
import json
import math
import os
import random
import tempfile
import unittest

import numpy as np

from bot.ai import lh_features as lf
from bot.ai import long_horizon as lh

DAYS = 480
T0 = lh.DECISION_END_MS - DAYS * lh.DAY
CREATED = "2026-09-25T00:00:00Z"


def walk(n=(DAYS + 3) * 24, seed=1, t0=T0, vol=0.006, drift=0.0, regime_days=None, p=100.0):
    rng = random.Random(seed)
    out, mu = [], 0.0
    for i in range(n):
        if regime_days and i % (regime_days * 24) == 0:
            mu = drift * rng.choice((-1, 1))
        o, p = p, max(1.0, p * math.exp(mu + rng.gauss(0, vol)))
        h = max(o, p) * (1 + abs(rng.gauss(0, vol / 3)))
        lo = min(o, p) * (1 - abs(rng.gauss(0, vol / 3)))
        out.append({"ts": t0 + i * lf.H1, "o": o, "h": h, "l": lo, "c": p, "v": 1000.0})
    return out


WINDOW = {"decision_start_ms": T0 + 75 * lh.DAY, "decision_end_ms": lh.DECISION_END_MS}
_CACHE = {}


def run_on(key):
    if key not in _CACHE:
        kw = {"noise": {}, "trend": {"drift": 0.0025, "regime_days": 12}}[key]
        data = {s: walk(seed=k + 1, **kw) for k, s in enumerate(("BTCUSDT", "XRPUSDT", "ADAUSDT"))}
        ctx = lh.symbol_context(data)
        _CACHE[key] = (data, ctx, lh.run(data, WINDOW, parity_ok=True, arch_parity_ok=True, sym_ctx=ctx))
    return _CACHE[key]


# ── trade-level fixture for search / gate / classification ────────────────
SYMS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT")
TID = lh.tuple_id("h4", "HTF_TREND_CONTINUATION", "LONG", "SIGNAL_CLOSE", "H4_ATR_1_5", "TIME_24H")
SPAN = (lh.DECISION_END_MS - 730 * lh.DAY, lh.DECISION_END_MS)


def block(n=2400, seed=3, base=0.25, signal=0.8, stress_cost=0.1):
    rng = np.random.default_rng(seed)
    ts = (SPAN[0] + np.arange(n) * ((SPAN[1] - SPAN[0] - 4 * lh.DAY) // n)).astype(np.int64)
    x = rng.normal(size=(n, len(lf.MODEL_FEATURES)))
    r = base + signal * x[:, 0] + rng.normal(0, 0.7, n)
    b = {"ts": ts, "end_ts": ts + 12 * lf.H1, "symbol": np.array([SYMS[i % 6] for i in range(n)]),
         "regime": np.array([("TREND_UP", "RANGE", "MIXED")[i % 3] for i in range(n)]),
         "r": r, "gross_r": r + 0.3, "hold_h": np.full(n, 12.0), "stop_frac": np.full(n, 0.02), "x": x,
         "fee_r": np.full(n, 0.06), "slip_r": np.full(n, 0.08), "fund_r": np.full(n, 0.01),
         "n_settle": np.full(n, 1)}
    for k in lh.STRESS:
        b[k] = r - stress_cost
    b["month"] = lh._month(ts)
    return b


def kept_from(b):
    lay = lh.fold_layout(*SPAN)
    b = dict(b)
    b["fold"] = lh.fold_of(b, lay)
    b["pre"] = [lh.pre_gate(b, np.isin(b["fold"], tr)) for tr, _, _ in lh.STEPS]
    return {TID: b}


def search_on(b):
    k = kept_from(b)
    s = lh.search(k, [])
    return k, s, lh.gate(s["steps"], parity_ok=True, arch_parity_ok=True)


class Data(unittest.TestCase):
    def test_01_pagination_never_exceeds_exchange_maximum(self):
        bars = walk(n=2000)
        calls = []

        async def page(sym, a, b):
            got = [x for x in bars if a <= x["ts"] <= b][:lh.EXCHANGE_MAX_KLINES]
            calls.append(((b - a) // lf.H1, len(got)))
            random.Random(len(calls)).shuffle(got)
            return got
        out, meta = asyncio.run(lh.fetch_series(page, "BTCUSDT", bars[0]["ts"], bars[-1]["ts"] + lf.H1))
        self.assertEqual([b["ts"] for b in out], [b["ts"] for b in bars])
        self.assertTrue(all(span <= lh.FETCH_PAGE_BARS for span, _ in calls))
        self.assertLessEqual(meta["max_returned_per_page"], lh.EXCHANGE_MAX_KLINES)

        async def too_big(sym, a, b):
            return bars[:lh.EXCHANGE_MAX_KLINES + 1]
        with self.assertRaises(RuntimeError):
            asyncio.run(lh.fetch_series(too_big, "BTCUSDT", bars[0]["ts"], bars[-1]["ts"]))

    def test_02_incomplete_coverage_fails_closed(self):
        bars = walk(n=(730 + 75 + 3) * 24, t0=lh.DECISION_END_MS - (730 + 75) * lh.DAY)
        start, end = bars[0]["ts"], bars[-1]["ts"] + lf.H1
        self.assertTrue(lh.coverage(bars, start, end)["ok"])
        holey = [b for i, b in enumerate(bars) if (i // 150) % 2 == 0]
        self.assertFalse(lh.coverage(holey, start, end)["ok"])
        raw = {s: holey for s in lh.SYMBOLS}
        self.assertEqual(lh.select_window(raw)["status"], "INSUFFICIENT_COVERAGE")
        short = {s: bars[-(400 * 24):] for s in lh.SYMBOLS}               # only ~400 days available
        w = lh.select_window(short)
        self.assertEqual((w["status"], w.get("days")), ("INSUFFICIENT_COVERAGE", None))
        mid = {s: bars[-((365 + 75 + 3) * 24):] for s in lh.SYMBOLS}
        self.assertEqual(lh.select_window(mid)["days"], 365)

    def test_19_exact_dataset_hash_preservation(self):
        d = {"BTCUSDT": walk(n=500)}
        p = os.path.join(tempfile.mkdtemp(), "d.json.gz")
        sha = lh.save_dataset(d, p)
        back, hdr = lh.load_dataset(p)
        self.assertEqual(lh.dataset_sha256(back), sha)
        with gzip.open(p, "rt") as fh:
            raw = json.load(fh)
        raw["data"]["BTCUSDT"][100][4] += 1e-6
        with gzip.open(p, "wt") as fh:
            json.dump(raw, fh)
        with self.assertRaises(ValueError):
            lh.load_dataset(p)

    def test_forward_cutoff(self):
        d = {"BTCUSDT": walk(n=10, t0=lh.FORWARD_EVIDENCE_CUTOFF_MS - 5 * lf.H1)}
        with self.assertRaises(lh.ForwardEvidenceRefused):
            lh.check_no_forward_evidence(d)


class Features(unittest.TestCase):
    def test_03_no_future_candle_leakage(self):
        c = walk(n=3000)
        F = lf.series(c)
        cut = 2500
        mut = copy.deepcopy(c)
        for b in mut[cut:]:
            b["c"] *= 1.5
            b["h"] *= 1.6
        G = lf.series(mut)
        for k in lf.RAW_KEYS:
            np.testing.assert_array_equal(F[k][:cut + 1], G[k][:cut + 1])
        T = c[cut]["ts"]
        a, b = lf.runtime_raw(c, T), lf.runtime_raw(mut, T)
        self.assertEqual({k: (None if math.isnan(v) else v) for k, v in a.items()},
                         {k: (None if math.isnan(v) else v) for k, v in b.items()})

    def _parity(self, tf):
        c = walk(n=4000)
        rep = lf.parity_report(c)
        self.assertTrue(rep["parity"])
        self.assertGreater(rep["per_timeframe"][tf]["values_checked"], 0)
        self.assertEqual(rep["per_timeframe"][tf]["mismatches"], 0)

        def broken(c1h, T, idx=None):
            out = lf.runtime_raw(c1h, T, idx)
            out[f"{tf}_atr14"] *= 1.001
            return out
        bad = lf.parity_report(c, runtime=broken)
        self.assertFalse(bad["parity"])
        self.assertGreater(bad["per_timeframe"][tf]["mismatches"], 0)

    def test_04_h1_feature_parity(self):
        self._parity("h1")

    def test_05_h4_feature_parity(self):
        self._parity("h4")

    def test_06_d1_feature_parity(self):
        self._parity("d1")

    def test_20_runtime_architecture_parity(self):
        c = walk(n=4000)
        F = lf.series(c)
        rep = lh.architecture_parity(c, F, per_signal=3)
        self.assertTrue(rep["parity"], rep["examples"])
        self.assertGreater(rep["events_checked"], 20)
        self.assertGreater(rep["stops_checked"], 0)
        bad = lh.architecture_parity(c, F, per_signal=3, runtime=lambda *a, **k: False)
        self.assertFalse(bad["parity"])


class Economics(unittest.TestCase):
    def test_07_synthetic_long_horizon_trend_detected(self):
        _, _, noise = run_on("noise")
        _, _, trend = run_on("trend")
        fam_t = trend["acc"].report("family")["HTF_TREND_CONTINUATION"]
        fam_n = noise["acc"].report("family")["HTF_TREND_CONTINUATION"]
        self.assertGreater(fam_t["mean_gross_r"], 0.1)
        self.assertGreater(fam_t["mean_net_r"], 0)
        self.assertGreater(fam_t["mean_net_r"], fam_n["mean_net_r"])

    def test_08_random_walk_negative_after_costs(self):
        _, _, res = run_on("noise")
        rep = res["acc"].report("trigger")
        self.assertTrue(all(v["mean_net_r"] < 0 for v in rep.values()))
        self.assertNotEqual(res["result"], "CANDIDATE_SUPPORTED")          # chance pre-gate passes never survive

    def test_09_wider_stop_lowers_cost_r_without_more_money_at_risk(self):
        _, _, res = run_on("noise")
        st = res["acc"].report("stop")
        self.assertLess(st["STRUCT_SWING"]["total_cost_r"], st["H1_ATR_2_0"]["total_cost_r"])
        self.assertGreater(st["STRUCT_SWING"]["mean_stop_pct"], st["H1_ATR_2_0"]["mean_stop_pct"])
        data, ctx, _ = run_on("noise")
        blocks = lh.generate_signal(ctx, "h1", "HTF_BREAKOUT", "LONG", span=(WINDOW["decision_start_ms"],
                                                                             WINDOW["decision_end_ms"]),
                                    entries=("SIGNAL_CLOSE",), exits=("TIME_24H",))
        tight = blocks[lh.tuple_id("h1", "HTF_BREAKOUT", "LONG", "SIGNAL_CLOSE", "H1_ATR_2_0", "TIME_24H")]
        wide = blocks[lh.tuple_id("h1", "HTF_BREAKOUT", "LONG", "SIGNAL_CLOSE", "STRUCT_SWING", "TIME_24H")]
        self.assertLess(np.mean(1 / wide["stop_frac"]), np.mean(1 / tight["stop_frac"]))   # smaller position
        for b in (tight, wide):                                                           # loss capped at 1R (+gap)
            no_gap = b["gross_r"][b["gross_r"] < 0]
            self.assertGreaterEqual(np.percentile(no_gap, 1), -1.0 - 0.25)

    def test_10_funding_scales_with_holding_duration(self):
        h = lf.H1
        base = 1_000 * 8 * h
        self.assertEqual(int(lh.settlements(base + 7 * h, base + 9 * h)), 1)
        self.assertEqual(int(lh.settlements(base, base + 8 * h)), 1)
        self.assertEqual(int(lh.settlements(base + h, base + 49 * h)), 6)
        _, _, res = run_on("noise")
        hz = res["acc"].report("holding_horizon")
        self.assertGreater(hz["TIME_48H"]["mean_settlements"], 2 * hz["TIME_12H"]["mean_settlements"])
        self.assertGreater(hz["TIME_48H"]["funding_r"], hz["TIME_12H"]["funding_r"])


class Selection(unittest.TestCase):
    def test_planted_edge_supported(self):
        _, s, g = search_on(block())
        self.assertTrue(g["all_pass"], g["failures"])
        self.assertEqual(lh.classify(g, [[], []])[0], "CANDIDATE_SUPPORTED")

    def test_11_cost_stress_veto(self):
        _, s, g = search_on(block(stress_cost=2.0))
        self.assertFalse(g["all_pass"])
        self.assertTrue(any(f.startswith("COST_STRESS_FAILS") for f in g["failures"]) or
                        "ABSTAIN_IN_SOME_STEP" in g["failures"])
        steps = s["steps"]
        if all(st["selected"] for st in steps):
            self.assertIn("COST_STRESS_FAILS_COMBINED_ADVERSE", g["failures"])

    def test_12_insufficient_sample_returns_insufficient_evidence(self):
        g = {"all_pass": False, "failures": ["TOO_FEW_POOLED_TRADES", "EXPECTANCY_CI_NOT_ESTIMABLE"],
             "pooled_approved": {"n": 25, "mean_r": 0.4}}
        self.assertEqual(lh.classify(g, [[], []])[0], "INSUFFICIENT_EVIDENCE")
        few = [{"n": 20, "pass": False, "only_sample_failures": True}] * 10
        g2 = {"all_pass": False, "failures": ["ABSTAIN_IN_SOME_STEP"], "pooled_approved": {"n": 0}}
        self.assertEqual(lh.classify(g2, [few, few])[0], "INSUFFICIENT_EVIDENCE")
        many = [{"n": 200, "pass": False, "only_sample_failures": False}] * 10
        self.assertEqual(lh.classify(g2, [many, many])[0], "NO_VALID_CHALLENGER")
        g3 = {"all_pass": False, "failures": ["TOO_FEW_POOLED_TRADES", "EXPECTANCY_NOT_POSITIVE"],
              "pooled_approved": {"n": 25, "mean_r": -0.2}}
        self.assertEqual(lh.classify(g3, [many, many])[0], "NO_VALID_CHALLENGER")

    def test_13_outer_outcomes_cannot_alter_inner_selection(self):
        b = block()
        k, s, _ = search_on(b)
        f4 = k[TID]["fold"] == 3
        mut = copy.deepcopy(b)
        vals = mut["r"][f4].copy()
        np.random.default_rng(5).shuffle(vals)
        mut["r"][f4] = -vals
        _, s2, _ = search_on(mut)
        self.assertEqual(s2["steps"][1]["selected"], s["steps"][1]["selected"])

    def test_14_future_mutation_cannot_alter_earlier_decisions(self):
        b = block()
        k, s, _ = search_on(b)
        later = np.isin(k[TID]["fold"], [2, 3])
        mut = copy.deepcopy(b)
        mut["r"][later] = -mut["r"][later]
        _, s2, _ = search_on(mut)
        self.assertEqual(s2["steps"][0]["selected"], s["steps"][0]["selected"])
        # candle level: trades that closed before the mutation are identical
        data, ctx, _ = run_on("noise")
        cut = WINDOW["decision_start_ms"] + 200 * lh.DAY
        mdata = {s_: [dict(x, c=x["c"] * 1.3, h=x["h"] * 1.4) if x["ts"] >= cut else x for x in v]
                 for s_, v in data.items()}
        span = (WINDOW["decision_start_ms"], WINDOW["decision_end_ms"])
        a = lh.generate_signal(ctx, "h1", "HTF_PULLBACK", "LONG", span=span, exits=("TIME_24H",))
        bb = lh.generate_signal(lh.symbol_context(mdata), "h1", "HTF_PULLBACK", "LONG", span=span,
                                exits=("TIME_24H",))
        for tid in a:
            ea, eb = a[tid]["end_ts"] < cut, bb[tid]["end_ts"] < cut
            np.testing.assert_array_equal(a[tid]["ts"][ea], bb[tid]["ts"][eb])
            np.testing.assert_array_equal(a[tid]["r"][ea], bb[tid]["r"][eb])

    def test_15_concentration_gate_veto(self):
        b = block()
        b["symbol"] = np.array(["BTCUSDT"] * len(b["r"]))
        _, s, g = search_on(b)
        self.assertFalse(g["all_pass"])
        self.assertTrue({"SINGLE_SYMBOL_DOMINATES", "ABSTAIN_IN_SOME_STEP"} & set(g["failures"]))


class Portfolio(unittest.TestCase):
    def test_16_overlap_and_portfolio_risk_caps(self):
        t0 = lh.DECISION_END_MS - 100 * lh.DAY
        trades = []
        for i in range(60):
            for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT"):
                for dup in range(2):
                    trades.append({"ts": t0 + i * lh.DAY, "end_ts": t0 + i * lh.DAY + 30 * lf.H1, "symbol": s,
                                   "r": 0.5 if (i + dup) % 2 else -1.0, "stop_frac": 0.02, "hold_h": 30.0})
        p = lh.portfolio(trades)
        self.assertLessEqual(p["max_concurrent_seen"], lh.PORTFOLIO["max_concurrent"])
        self.assertGreater(p["skipped"].get("MAX_CONCURRENT", 0) + p["skipped"].get("MAX_PER_SYMBOL", 0), 0)
        tight = dict(lh.PORTFOLIO, max_open_risk=0.02)
        self.assertLessEqual(lh.portfolio(trades, tight)["max_concurrent_seen"], 2)
        small_stop = [dict(t, stop_frac=0.001) for t in trades]                 # 10x notional per risk
        q = lh.portfolio(small_stop)
        self.assertGreater(q["skipped"].get("MAX_NOTIONAL", 0), 0)


class Freeze(unittest.TestCase):
    def _supported(self):
        k, s, g = search_on(block())
        return {"result": "CANDIDATE_SUPPORTED" if g["all_pass"] else "NO_VALID_CHALLENGER", "steps": s["steps"]}

    def test_17_failed_gate_cannot_freeze(self):
        with self.assertRaises(ValueError):
            lh.freeze({"result": "NO_VALID_CHALLENGER"}, dataset_sha="d" * 64, code_sha="c" * 40, created_at=CREATED)
        with self.assertRaises(ValueError):
            lh.freeze({"result": "INSUFFICIENT_EVIDENCE"}, dataset_sha="d" * 64, code_sha="c" * 40, created_at=CREATED)

    def test_18_deterministic_bundle_roundtrip(self):
        res = self._supported()
        self.assertEqual(res["result"], "CANDIDATE_SUPPORTED")
        b1 = lh.freeze(res, dataset_sha="d" * 64, code_sha="c" * 40, created_at=CREATED)
        b2 = lh.freeze(res, dataset_sha="d" * 64, code_sha="c" * 40, created_at=CREATED)
        self.assertEqual(b1["manifest"]["bundle_sha256"], b2["manifest"]["bundle_sha256"])
        self.assertEqual(b1["manifest"]["schema"], "LONG_HORIZON_ARCHITECTURE_BUNDLE_V1")
        self.assertFalse(b1["policy"]["order_authority"] or b1["policy"]["live_authority"]
                         or b1["policy"]["execution_lease"])
        tmp = tempfile.mkdtemp()
        lh.export(b1, tmp)
        v1, v2 = lh.verify(tmp), lh.verify(tmp)
        self.assertTrue(v1["verified"], v1["failures"])
        self.assertEqual(v1, v2)
        with open(os.path.join(tmp, "policy.json")) as fh:
            pol = json.load(fh)
        pol["order_authority"] = True
        with open(os.path.join(tmp, "policy.json"), "w") as fh:
            json.dump(pol, fh)
        self.assertFalse(lh.verify(tmp)["verified"])


class Spec(unittest.TestCase):
    def test_spec_bounded_and_hashed(self):
        self.assertEqual(lh.architecture_count(), 2 * 6 * 2 * 4 * 5 * 7)
        self.assertEqual(len(lh.SPEC_SHA256), 64)
        self.assertEqual(lh.EVIDENCE_LABEL, "PREVIOUSLY_INSPECTED_RESEARCH_SUPPORT")
        self.assertEqual(lh.DECISION_END_MS % lf.H1, 0)
        self.assertNotIn("m15", lh.TRIGGERS)
        dt.datetime.fromtimestamp(lh.DECISION_END_MS / 1000, dt.timezone.utc)


if __name__ == "__main__":
    unittest.main()
