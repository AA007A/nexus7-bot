"""Phase 8G-A: data feasibility audit (no outcomes, no models)."""
import asyncio
import copy
import inspect
import math
import random
import unittest

import numpy as np

from bot.ai import alpha_audit as aa
from bot.ai import alpha_audit_report as ar
from bot.ai import xsec_features as xf

H1 = aa.H1
T0 = 1_750_000_000_000 // H1 * H1
SYMS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT")


def walk(n=24 * 45, seed=1, t0=T0):
    rng = random.Random(seed)
    out, p = [], 100.0
    for i in range(n):
        o, p = p, p * math.exp(rng.gauss(0, 0.006))
        out.append({"ts": t0 + i * H1, "o": o, "h": max(o, p) * 1.001, "l": min(o, p) * 0.999, "c": p,
                    "v": 1000 * (1 + abs(rng.gauss(0, 1)))})
    return out


def universe(n=24 * 45):
    return {s: walk(n=n, seed=k + 1) for k, s in enumerate(SYMS)}


class FakeHttp:
    """Serves a kline series with an exchange cap; records request spans."""

    def __init__(self, bars, cap=200, fail=False):
        self.bars, self.cap, self.fail, self.spans = bars, cap, fail, []

    async def get(self, url, params=None, **k):
        if self.fail:
            return 503, {"code": "503000", "msg": "unavailable"}, {}
        a, b = int(params["from"]), int(params["to"])
        self.spans.append((b - a) // H1)
        rows = [[x["ts"], x["o"], x["h"], x["l"], x["c"], x["v"]] for x in self.bars if a <= x["ts"] <= b][:self.cap]
        random.Random(a).shuffle(rows)
        return 200, {"code": "200000", "data": rows}, {}


class Pagination(unittest.TestCase):
    def test_pagination_respects_exchange_cap_and_recovers_every_bar(self):
        bars = walk(n=2000)
        http = FakeHttp(bars, cap=200)
        got, meta = asyncio.run(aa.kucoin_klines(http, ".KXBTUSDT", bars[0]["ts"], bars[-1]["ts"] + H1))
        self.assertEqual([g[0] for g in got], [b["ts"] for b in bars])
        self.assertTrue(all(s <= 150 for s in http.spans))
        self.assertLessEqual(meta["max_rows_per_page"], 200)


class Quality(unittest.TestCase):
    def test_duplicate_timestamps_detected_and_deduplicated(self):
        ts = [0, H1, H1, 2 * H1]
        q = aa.series_quality(ts, [1, 2, 2, 3], step_ms=H1, start=0, end=3 * H1)
        self.assertEqual(q["duplicates"], 1)
        self.assertEqual(q["non_monotonic"], 1)
        rows = [["0", "1", "1", "1", "1", "1", "0", "1", "1", "0.5"], ["0", "2", "2", "2", "2", "2", "0", "2", "2", "1"]]
        self.assertEqual(len(aa.parse_vision_klines(rows)), 1)

    def test_missing_intervals(self):
        ts = [k * H1 for k in range(100) if k not in (10, 11, 12, 50)]
        q = aa.series_quality(ts, None, step_ms=H1, start=0, end=100 * H1)
        self.assertEqual(q["gap_count"], 2)
        self.assertEqual(q["max_gap_intervals"], 3)
        self.assertAlmostEqual(q["coverage"], 0.96)

    def test_incomplete_history_classification(self):
        base = dict(availability="VERIFIED", lookahead_risk="PASS", parity="PARITY_READY", cost_class="FREE_PUBLIC",
                    point_in_time=True, coverage=0.999)
        self.assertEqual(aa.classify_source(dict(base, history_days=548)), "READY_FOR_8G_B")
        self.assertEqual(aa.classify_source(dict(base, history_days=200, live_endpoint_verified=True)), "PROSPECTIVE_ONLY")
        self.assertEqual(aa.classify_source(dict(base, history_days=200)), "REJECTED")
        self.assertEqual(aa.classify_source(dict(base, history_days=548, parity="CROSS_SOURCE_PROXY")), "NEEDS_ENGINEERING")
        self.assertEqual(aa.classify_source(dict(base, history_days=548, lookahead_risk="FAIL")), "REJECTED")
        self.assertEqual(aa.classify_source(dict(base, history_days=548, coverage=0.90)), "REJECTED")


class Timestamps(unittest.TestCase):
    def test_source_timestamp_normalization(self):
        ms = 1_750_000_000_000
        for v in (ms // 1000, ms, ms * 1000, ms * 1_000_000, str(ms)):
            self.assertEqual(aa.to_ms(v), ms)
        self.assertEqual(aa.to_ms("2025-06-15T15:06:40Z"), 1_750_000_000_000)

    def test_event_ts_vs_observed_ts(self):
        f = aa.norm_kucoin_funding({"timepoint": T0, "fundingRate": 0.0001}, symbol="BTCUSDT", granularity_ms=aa.H8,
                                   retrieved_at=T0 + 1)
        self.assertEqual((f["event_ts"], f["observed_ts"], f["interval_start"]), (T0, T0, T0 - aa.H8))
        k = aa.norm_kline_bar(T0, 100.0, symbol="BTCUSDT", metric="index_close", source="S", venue="V", endpoint="e",
                              venue_symbol=".KXBTUSDT", bar_ms=H1, retrieved_at=T0, raw=[T0, 100])
        self.assertEqual((k["event_ts"], k["observed_ts"], k["interval_start"]), (T0 + H1, T0 + H1, T0))
        m = aa.norm_binance_metric({"create_time": T0, "symbol": "BTCUSDT", "sum_open_interest": "5"},
                                   metric="sum_open_interest", retrieved_at=T0)
        self.assertEqual(m["observed_ts"] - m["event_ts"], aa.CONSERVATIVE_SNAPSHOT_LAG_MS)
        self.assertIn("CONSERVATIVE_LAG", m["quality_flags"])

    def test_lookahead_rejection(self):
        recs = [aa.norm_kucoin_funding({"timepoint": T0 + k * aa.H8, "fundingRate": 1e-4}, symbol="BTCUSDT",
                                       granularity_ms=aa.H8, retrieved_at=T0) for k in range(5)]
        D = T0 + 2 * aa.H8 + 1
        self.assertEqual(len(aa.visible(recs, D)), 3)
        with self.assertRaises(aa.LookaheadRejected):
            aa.assert_point_in_time(recs, D)
        aa.assert_point_in_time(aa.visible(recs, D), D)


class Symbols(unittest.TestCase):
    def test_symbol_normalization(self):
        self.assertEqual(aa.kucoin_symbol("BTCUSDT"), "XBTUSDTM")
        self.assertEqual(aa.kucoin_symbol("SOLUSDT"), "SOLUSDTM")
        self.assertEqual(aa.canonical_symbol("XBTUSDTM"), "BTCUSDT")
        self.assertEqual(aa.canonical_symbol("BTC-USDT-SWAP"), "BTCUSDT")
        for s in aa.SYMBOLS:
            self.assertEqual(aa.canonical_symbol(aa.kucoin_symbol(s)), s)


class Determinism(unittest.TestCase):
    def test_deterministic_dataset_hashing(self):
        d = {"BTCUSDT": [[1, 2.0], [2, 3.0]], "ETHUSDT": [[1, 1.0]]}
        m1 = aa.manifest("x", source="s", metric_set=["m"], symbols=list(d), rows_by_symbol=d, canonical=d, retrieved_at=0)
        d2 = {"ETHUSDT": [[1, 1.0]], "BTCUSDT": [[1, 2.0], [2, 3.0]]}
        m2 = aa.manifest("x", source="s", metric_set=["m"], symbols=list(d2), rows_by_symbol=d2, canonical=d2, retrieved_at=0)
        self.assertEqual(m1["sha256"], m2["sha256"])
        d2["BTCUSDT"][0][1] = 2.0000001
        m3 = aa.manifest("x", source="s", metric_set=["m"], symbols=list(d2), rows_by_symbol=d2, canonical=d2, retrieved_at=0)
        self.assertNotEqual(m1["sha256"], m3["sha256"])
        self.assertEqual(len(aa.ALPHA_DATA_CONTRACT_SHA256), 64)

    def test_deterministic_cross_sectional_features(self):
        u = universe()
        a, b = xf.matrix(u), xf.matrix(copy.deepcopy(u))
        self.assertEqual(xf.features_sha256(a), xf.features_sha256(b))


class Leakage(unittest.TestCase):
    def test_no_future_cross_section_member_leaks_backwards(self):
        u = universe()
        T = xf.grid(u)
        cut = T[len(T) // 2]
        before = xf.matrix(u, T[T <= cut])
        mut = copy.deepcopy(u)
        for b in mut["SOLUSDT"]:
            if b["ts"] + H1 > cut:
                b["c"] *= 3.0
                b["v"] *= 10
        mut["NEWUSDT"] = [dict(b) for b in walk(seed=99) if b["ts"] + H1 > cut]    # member that only exists later
        after = xf.matrix(mut, T[T <= cut])
        for s in u:
            for f in xf.PER_SYMBOL:
                np.testing.assert_array_equal(before["per_symbol"][s][f], after["per_symbol"][s][f])
        for f in xf.MARKET:
            np.testing.assert_array_equal(before["market"][f], after["market"][f])

    def test_runtime_replay_equivalence(self):
        u = universe()
        del u["ETHUSDT"][700:703]
        rep = xf.parity_report(u, samples=40)
        self.assertTrue(rep["parity"], rep["examples"])

        def broken(data, T, idx=None):
            per, mk = xf.runtime_at(data, T, idx)
            mk = dict(mk, dispersion_24h=mk["dispersion_24h"] * 1.01)
            return per, mk
        self.assertFalse(xf.parity_report(u, samples=40, runtime=broken)["parity"])
        raw = {"timepoint": T0, "fundingRate": "0.0001"}
        hist = aa.norm_kucoin_funding(raw, symbol="BTCUSDT", granularity_ms=aa.H8, retrieved_at=T0 + 5)
        live = aa.norm_kucoin_funding(dict(raw), symbol="BTCUSDT", granularity_ms=aa.H8, retrieved_at=T0 + 5)
        self.assertEqual(hist, live)


class Missing(unittest.TestCase):
    def test_source_failure_is_not_zeros(self):
        http = FakeHttp([], fail=True)
        rows, meta = asyncio.run(aa.kucoin_funding_history(http, "BTCUSDT", T0, T0 + 10 * aa.DAY))
        self.assertEqual(rows, [])
        self.assertGreater(meta["error_count"], 0)
        q = aa.series_quality([], [], step_ms=aa.H8, start=T0, end=T0 + 10 * aa.DAY)
        self.assertEqual(q["coverage"], 0.0)

    def test_unavailable_data_remains_missing(self):
        r = aa.norm_kucoin_funding({"timepoint": T0, "fundingRate": None}, symbol="BTCUSDT", granularity_ms=aa.H8,
                                   retrieved_at=T0)
        self.assertIsNone(r["value"])
        self.assertIn("MISSING", r["quality_flags"])
        u = universe()
        gap_t = u["SOLUSDT"][800]["ts"] + H1
        del u["SOLUSDT"][800]
        M = xf.matrix(u, np.array([gap_t]))
        self.assertTrue(np.isnan(M["per_symbol"]["SOLUSDT"]["ret_1h"][0]))
        self.assertEqual(M["market"]["n_available"][0], 4.0)


class Safety(unittest.TestCase):
    def test_no_private_credentials_required(self):
        src = inspect.getsource(aa) + inspect.getsource(ar)
        for token in ("KC-API", "X-MBX-APIKEY", "api_secret", "passphrase", "os.environ", "Authorization", "/api/v1/orders"):
            self.assertNotIn(token, src)
        self.assertIn("Public GET only", inspect.getsource(aa.Http))


if __name__ == "__main__":
    unittest.main()
