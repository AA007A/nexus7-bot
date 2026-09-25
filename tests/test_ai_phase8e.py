"""Phase 8E: SIGNAL_ARCHITECTURE_SPEC_V1 (research branch only)."""
import copy
import gzip
import json
import os
import random
import tempfile
import unittest

import numpy as np

from bot.ai import arch_features as af
from bot.ai import signal_architecture as sa

SCRATCH_T0 = sa.DECISION_START_MS - 200 * sa.BAR_MS
CREATED = "2026-09-25T00:00:00Z"
SHA = "c" * 40


def walk(n=17280, seed=1, t0=SCRATCH_T0, vol=0.004, ac=0.0, p=100.0, ou=None):
    """15m candles; ac = lag-1 autocorrelation of returns (momentum); ou = mean-reversion
    coefficient of log price towards 0 (planted reversion)."""
    import math
    rng = random.Random(seed)
    out, prev, x = [], 0.0, 0.0
    for i in range(n):
        ret = ac * prev + rng.gauss(0, vol)
        prev = ret
        if ou is not None:
            x = (1 - ou) * x + rng.gauss(0, vol)
            o, p = p, 100.0 * math.exp(x)
        else:
            o, p = p, max(1.0, p * (1 + ret))
        h = max(o, p) * (1 + abs(rng.gauss(0, vol / 3)))
        lo = min(o, p) * (1 - abs(rng.gauss(0, vol / 3)))
        out.append({"ts": t0 + i * sa.BAR_MS, "o": o, "h": h, "l": lo, "c": p, "v": 1000 * (1 + abs(rng.gauss(0, 1)))})
    return out


def agg(c15, k):
    out = []
    for j in range(0, len(c15) - k + 1, k):
        g = c15[j:j + k]
        out.append({"ts": g[0]["ts"], "o": g[0]["o"], "h": max(b["h"] for b in g), "l": min(b["l"] for b in g),
                    "c": g[-1]["c"], "v": sum(b["v"] for b in g)})
    return out


def dataset(symbols=("BTCUSDT", "XRPUSDT", "ADAUSDT"), **kw):
    out = {}
    for k, s in enumerate(symbols):
        c = walk(seed=k + 1, **kw)
        out[s] = {"15": c, "60": agg(c, 4), "240": agg(c, 16)}
    return out


# ── trades-level fixture (search / gate / freeze) ─────────────────────────
SYMS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT")
UNIT = ("BREAKOUT_CONT", "IMMEDIATE", "ATR_1_5", "LONG")


def trades(n=4000, seed=3, base=0.2, signal=0.8, unit=UNIT):
    rng = random.Random(seed)
    span = sa.DECISION_END_MS - sa.DECISION_START_MS - 2 * 86_400_000
    out = []
    for i in range(n):
        ts = sa.DECISION_START_MS + int(i * span / n)
        x = [rng.gauss(0, 1) for _ in af.MODEL_FEATURES]
        r = base + signal * x[0] + rng.gauss(0, 0.7)
        import datetime as dt
        out.append({"ts": ts, "event_ts": ts, "outcome_end_ts": ts + 4 * 3_600_000, "outcome_status": "RESOLVED",
                    "symbol": SYMS[i % len(SYMS)], "direction": unit[3], "setup": unit[0], "entry": unit[1],
                    "stop": unit[2], "unit": sa.unit_id(*unit), "ai_regime": ("TREND_UP", "RANGE", "MIXED")[i % 3],
                    "month": dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).strftime("%Y-%m"),
                    "gross_r": r + 0.3, "r": r, "x": x,
                    "cost_r": {"current": r, "fees_plus_50pct": r - 0.03, "slippage_x2": r - 0.04,
                               "combined_adverse": r - 0.08}})
    return out


_CACHE = {}


def planted():
    if "p" not in _CACHE:
        t = trades()
        _CACHE["p"] = (t, sa.run_search(t, parity_ok=True))
    return _CACHE["p"]


class Spec(unittest.TestCase):
    def test_spec_bounded_and_hashed(self):
        self.assertEqual(len(list(sa.units())), 6 * 4 * 5 * 2)
        self.assertEqual(len(sa.SPEC_SHA256), 64)
        self.assertEqual(sa.SPEC["supporting_gate"]["min_pooled_trades"], 60)
        self.assertEqual(sa.EVIDENCE_LABEL, "PREVIOUSLY_INSPECTED_RESEARCH_SUPPORT")


class Noise(unittest.TestCase):
    def test_01_noise_gives_no_challenger(self):
        d = dataset()
        tr = sa.generate(d)
        res = sa.run_search(tr, parity_ok=True)
        self.assertEqual(res["result"], "NO_VALID_CHALLENGER")
        self.assertLess(np.mean([t["r"] for t in tr]), 0)                     # costs make noise negative

    def test_02_wrong_direction_signal_rejected(self):
        t = trades(base=-0.6, signal=0.2)
        res = sa.run_search(t, parity_ok=True)
        self.assertEqual(res["result"], "NO_VALID_CHALLENGER")
        raw = [e for e in res["ledger"] if e["model"] == "RAW" and e["unit"] == sa.unit_id(*UNIT)]
        self.assertTrue(all(not e["pre_gate"]["pass"] for e in raw))
        self.assertIn("NET_MEAN_NOT_POSITIVE", raw[0]["pre_gate"]["failures"])


def _unit_mean(tr, setup, entry="IMMEDIATE", stop="ATR_1_5"):
    v = [t["r"] for t in tr if t["setup"] == setup and t["entry"] == entry and t["stop"] == stop]
    return float(np.mean(v)), len(v)


class PlantedMarkets(unittest.TestCase):
    def test_03_planted_trend_continuation_detected(self):
        d = dataset(vol=0.01, ac=0.45)
        tr = sa.generate(d, setups=("BREAKOUT_CONT", "RANGE_MEANREV"), entries=("IMMEDIATE",), stops=("ATR_1_5",))
        mom, n = _unit_mean(tr, "BREAKOUT_CONT")
        rev, _ = _unit_mean(tr, "RANGE_MEANREV")
        self.assertGreater(n, 100)
        self.assertGreater(mom, 0)
        self.assertGreater(mom, rev)

    def test_04_planted_mean_reversion_detected(self):
        d = dataset(vol=0.01, ou=0.05)
        tr = sa.generate(d, setups=("BREAKOUT_CONT", "RANGE_MEANREV"), entries=("IMMEDIATE",), stops=("ATR_1_5",))
        mom, _ = _unit_mean(tr, "BREAKOUT_CONT")
        rev, n = _unit_mean(tr, "RANGE_MEANREV")
        self.assertGreater(n, 30)
        self.assertGreater(rev, 0)
        self.assertGreater(rev, mom)


def _pattern_candles(kind, events=40, gap=60):
    """Flat baseline (range 1.0 -> atr 1.0) with a scripted path at each event bar."""
    c, p, t, ev = [], 100.0, SCRATCH_T0, []
    rng = random.Random(9)

    def bar(o, h, lo, cl):
        c.append({"ts": t + len(c) * sa.BAR_MS, "o": o, "h": h, "l": lo, "c": cl, "v": 1000.0})
    for _ in range(sa.af.W15 + 20):
        bar(p, p + 0.5, p - 0.5, p)
    for k in range(events):
        for q in range(gap):
            lo = p - (1.8 if (kind == "tight" and q == gap - 3) else 0.5)
            bar(p, p + 0.5, lo, p)
        ev.append(len(c))
        if kind == "tight":                       # dip 1.3 ATR intrabar, then rally 5.6 ATR
            bar(p, p + 0.3, p - 1.3, p + 0.2)
            p += 0.2
            for _ in range(8):
                bar(p, p + 0.8, p - 0.1, p + 0.7)
                p += 0.7
        else:                                     # 1/3 real (bar i bullish, rally), 2/3 fake (bar i bearish, fall)
            if rng.random() < 1 / 3:
                bar(p, p + 0.6, p - 0.1, p + 0.5)
                p += 0.5
                step = 0.6
            else:
                bar(p, p + 0.1, p - 1.3, p - 1.2)
                p -= 1.2
                step = -0.6
            for _ in range(8):
                bar(p, max(p, p + step) + 0.1, min(p, p + step) - 0.1, p + step)
                p += step
    for _ in range(80):
        bar(p, p + 0.5, p - 0.5, p)
    return c, ev


def _forced(kind, **kw):
    c, ev = _pattern_candles(kind)
    idx = set(ev)
    mask = np.array([i in idx for i in range(len(c))])
    over = lambda su, side: mask if side == "LONG" else np.zeros(len(c), bool)       # noqa: E731
    return sa.generate_symbol("BTCUSDT", c, agg(c, 4), agg(c, 16), span=(0, 10 ** 15), setups=("BREAKOUT_CONT",),
                              event_override=over, **kw)


class EntryAndStop(unittest.TestCase):
    def test_05_confirmation_improves_early_entries(self):
        tr = _forced("early", entries=("IMMEDIATE", "CONFIRM_1"), stops=("ATR_1_0",))
        imm = [t["r"] for t in tr if t["entry"] == "IMMEDIATE"]
        con = [t["r"] for t in tr if t["entry"] == "CONFIRM_1"]
        self.assertGreater(len(imm), 30)
        self.assertGreater(len(con), 5)
        self.assertLess(np.mean(imm), 0)
        self.assertGreater(np.mean(con), 0)
        eta = sa.entry_timing_analysis(tr)["by_setup_side_stop"]["BREAKOUT_CONT|LONG|ATR_1_0"]
        self.assertGreater(eta["CONFIRM_1"]["mean_r"], eta["IMMEDIATE"]["mean_r"])

    def test_06_structural_stop_recovers_edge_without_more_risk(self):
        tr = _forced("tight", entries=("IMMEDIATE",), stops=("ATR_1_0", "SWING_8"))
        tight = [t for t in tr if t["stop"] == "ATR_1_0"]
        swing = [t for t in tr if t["stop"] == "SWING_8"]
        self.assertLess(np.mean([t["r"] for t in tight]), 0)
        self.assertGreater(np.mean([t["r"] for t in swing]), 0)
        for rows in (tight, swing):                       # loss at stop is never more than 1R before costs
            self.assertGreaterEqual(min(t["gross_r"] for t in rows), -1.0 - 1e-9)
        self.assertLess(np.mean([t["notional_per_risk"] for t in swing]),
                        np.mean([t["notional_per_risk"] for t in tight]))     # wider stop => smaller size
        self.assertTrue(all(t["path"]["stopped_then_plus1r"] for t in tight))


class Selection(unittest.TestCase):
    def test_planted_edge_supported(self):
        _, res = planted()
        self.assertEqual(res["result"], "CANDIDATE_SUPPORTED", res["gate"]["failures"])

    def test_07_future_mutation_cannot_alter_prior_selection(self):
        t, res = planted()
        later = {id(x) for f in res["_folds"][2:] for x in f}
        mut = copy.deepcopy(t)
        for i, x in enumerate(t):
            if id(x) in later:
                mut[i]["r"] = -mut[i]["r"]
                mut[i]["cost_r"] = {k: -v for k, v in mut[i]["cost_r"].items()}
        res2 = sa.run_search(mut, parity_ok=True)
        self.assertEqual(res2["steps"][0]["selected"], res["steps"][0]["selected"])

    def test_08_outer_fold_cannot_alter_inner_selection(self):
        t, res = planted()
        f4 = {id(x) for x in res["_folds"][3]}
        mut = copy.deepcopy(t)
        idx = [i for i, x in enumerate(t) if id(x) in f4]
        vals = [mut[i]["r"] for i in idx]
        random.Random(2).shuffle(vals)
        for i, v in zip(idx, vals):
            mut[i]["r"] = v
        res2 = sa.run_search(mut, parity_ok=True)
        self.assertEqual(res2["steps"][1]["selected"], res["steps"][1]["selected"])


def _gate_steps(n=200, r=0.4, sym=None, month=None, cost=None, side2="LONG"):
    rng = random.Random(3)
    rows = []
    for i in range(n):
        ts = sa.DECISION_START_MS + i * 12 * 3_600_000
        v = r + rng.gauss(0, 0.3)
        rows.append({"ts": ts, "outcome_end_ts": ts + 3_600_000, "r": v, "symbol": sym or SYMS[i % 3],
                     "month": month or f"2026-0{3 + i % 4}", "ai_regime": ("TREND_UP", "RANGE", "MIXED")[i % 3],
                     "direction": "LONG", "cost_r": {k: (cost if cost is not None else v)
                                                     for k in ("current",) + sa.REQUIRED_COST_SCENARIOS}})
    base = [dict(x, r=rng.gauss(-0.5, 0.5)) for x in rows]
    sel = lambda side: {"setup": "BREAKOUT_CONT", "side": side, "candidate_id": "x"}   # noqa: E731
    return [{"selected": sel("LONG"), "test": rows[:n // 2] + base[:n // 2], "approved": rows[:n // 2]},
            {"selected": sel(side2), "test": rows[n // 2:] + base[n // 2:], "approved": rows[n // 2:]}]


class Gate(unittest.TestCase):
    def test_clean_gate_passes(self):
        g = sa.gate(_gate_steps(), parity_ok=True)
        self.assertTrue(g["all_pass"], g["failures"])

    def test_09_cost_stress_veto(self):
        g = sa.gate(_gate_steps(cost=-0.01), parity_ok=True)
        self.assertIn("COST_STRESS_FAILS_COMBINED_ADVERSE", g["failures"])

    def test_10_concentration_veto(self):
        self.assertIn("SINGLE_SYMBOL_DOMINATES", sa.gate(_gate_steps(sym="BTCUSDT"), parity_ok=True)["failures"])
        self.assertIn("SINGLE_MONTH_DOMINATES", sa.gate(_gate_steps(month="2026-04"), parity_ok=True)["failures"])

    def test_direction_instability_veto(self):
        self.assertIn("DIRECTION_UNSTABLE", sa.gate(_gate_steps(side2="SHORT"), parity_ok=True)["failures"])

    def test_11_feature_parity_failure_veto(self):
        self.assertIn("RUNTIME_FEATURE_PARITY_FAILED", sa.gate(_gate_steps(), parity_ok=False)["failures"])
        c = walk(n=3000)

        def broken(c15, c1h, c4h, ts):
            out = af.runtime_raw(c15, c1h, c4h, ts)
            out["atr14"] *= 1.001
            return out
        self.assertTrue(af.parity_report(c, agg(c, 4), agg(c, 16))["parity"])
        self.assertFalse(af.parity_report(c, agg(c, 4), agg(c, 16), runtime=broken)["parity"])

    def test_calibration_failure_removes_probability_authority(self):
        steps = _gate_steps()
        for s in steps:
            s["probability_authorizes"] = True
            s["test_calibration"] = {"beats_base_rate": False, "ece": 0.3}
        self.assertIn("CALIBRATION_FAILURE_WITH_PROBABILITY_AUTHORITY", sa.gate(steps, parity_ok=True)["failures"])


class Freeze(unittest.TestCase):
    def test_12_failed_gate_cannot_freeze(self):
        t = trades(base=-0.6, signal=0.2)
        res = sa.run_search(t, parity_ok=True)
        with self.assertRaises(ValueError):
            sa.freeze(res, dataset_sha="d" * 64, trades_sha="t" * 64, code_sha=SHA, created_at=CREATED)

    def test_13_deterministic_artifact_roundtrip(self):
        _, res = planted()
        b1 = sa.freeze(res, dataset_sha="d" * 64, trades_sha="t" * 64, code_sha=SHA, created_at=CREATED)
        b2 = sa.freeze(res, dataset_sha="d" * 64, trades_sha="t" * 64, code_sha=SHA, created_at=CREATED)
        self.assertEqual(b1["manifest"]["bundle_sha256"], b2["manifest"]["bundle_sha256"])
        self.assertEqual(b1["manifest"]["lifecycle_state"], "SHADOW_CHALLENGER")
        tmp = tempfile.mkdtemp()
        sa.export(b1, tmp)
        v1, v2 = sa.verify(tmp), sa.verify(tmp)
        self.assertTrue(v1["verified"], v1["failures"])
        self.assertEqual(v1, v2)
        with open(os.path.join(tmp, "model.json")) as fh:
            m = json.load(fh)
        m["threshold"] += 0.01
        with open(os.path.join(tmp, "model.json"), "w") as fh:
            json.dump(m, fh)
        self.assertFalse(sa.verify(tmp)["verified"])

    def test_14_exact_dataset_hash_preservation(self):
        d = dataset(symbols=("BTCUSDT",))
        p = os.path.join(tempfile.mkdtemp(), "d.json.gz")
        sha = sa.save_dataset(d, p)
        back, hdr = sa.load_dataset(p)
        self.assertEqual(sa.dataset_sha256(back), sha)
        self.assertEqual(hdr["sha256"], sha)
        self.assertEqual(sa.trades_sha256(sa.generate(back)), sa.trades_sha256(sa.generate(d)))
        with gzip.open(p, "rt") as fh:
            raw = json.load(fh)
        raw["data"]["BTCUSDT"]["15"][500][4] += 1e-6
        with gzip.open(p, "wt") as fh:
            json.dump(raw, fh)
        with self.assertRaises(ValueError):
            sa.load_dataset(p)

    def test_no_forward_evidence(self):
        d = dataset(symbols=("BTCUSDT",))
        d["BTCUSDT"]["15"].append(dict(d["BTCUSDT"]["15"][-1], ts=sa.FORWARD_EVIDENCE_CUTOFF_MS))
        with self.assertRaises(sa.ForwardEvidenceRefused):
            sa.check_no_forward_evidence(d)


class Coverage(unittest.TestCase):
    def test_incomplete_history_fails_closed(self):
        c = walk(n=1000)
        start, end = c[0]["ts"], c[-1]["ts"] + sa.BAR_MS
        self.assertTrue(sa.coverage(c, start, end, sa.BAR_MS)["ok"])
        holey = [b for i, b in enumerate(c) if (i // 200) % 2 == 0]        # every other 200-bar page lost
        self.assertFalse(sa.coverage(holey, start, end, sa.BAR_MS)["ok"])
        self.assertFalse(sa.coverage(c[500:], start, end, sa.BAR_MS)["ok"])


class NexusStops(unittest.TestCase):
    def test_nexus_stop_classification_runs(self):
        from tests.test_ai_phase8d import rows as p8d_rows
        res = sa.nexus_stop_analysis(p8d_rows(n=600, signal=0.0))
        self.assertIn(res["random_recovery_verdict"], ("RANDOM_RECOVERY_CONSISTENT", "UNAVAILABLE",
                                                       "NEXUS_DIRECTION_RECOVERS_MORE_THAN_FLIPPED",
                                                       "NEXUS_DIRECTION_RECOVERS_LESS_THAN_FLIPPED"))
        self.assertGreater(res["losers"], 0)


if __name__ == "__main__":
    unittest.main()
