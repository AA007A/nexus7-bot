"""Phase 8G-A: DATA FEASIBILITY & ALPHA SOURCE AUDIT (strict read-only research).

DATA AUDIT ONLY. This module never reads trade outcomes, never fits a model,
never ranks anything by future returns. It probes PUBLIC, key-free endpoints,
downloads what can be downloaded, and measures provenance, historical depth,
coverage, timestamp semantics, lookahead risk, cross-source consistency
(contemporaneous series vs series - NOT against returns) and storage.

Every claim carries its evidence level:
  VERIFIED   - measured by this run (HTTP status, returned rows, checksums);
  DOCUMENTED - taken from the provider's public API documentation / known
               behaviour and NOT independently verified here.
Unreachable or failing sources are recorded as missing - never as zeros.
"""
from __future__ import annotations

import asyncio
import csv
import datetime as dt
import hashlib
import io
import json
import math
import time
import zipfile
from collections import Counter, defaultdict

import numpy as np

VERSION = "PHASE_8G_A_ALPHA_DATA_AUDIT_V1"
H1, MIN5, H8, DAY = 3_600_000, 300_000, 28_800_000, 86_400_000
WINDOW_START_MS = 1_740_409_200_000                # Phase 8F decision start 2025-02-24T15:00Z
WINDOW_END_MS = 1_787_756_400_000                  # Phase 8F decision end   2026-08-26T15:00Z
FORWARD_EVIDENCE_CUTOFF_MS = 1_790_000_000_000
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT",
           "LTCUSDT", "NEARUSDT", "ATOMUSDT")
MIN_HISTORY_DAYS, IDEAL_HISTORY_DAYS = 365, 548
READY_COVERAGE, ENGINEERING_COVERAGE = 0.98, 0.95
DEPTH_PROBE_DAYS = (1095, 730, 548, 365, 180, 90, 30, 7, 1)

KUCOIN = "https://api-futures.kucoin.com"
BINANCE_FAPI = "https://fapi.binance.com"
BINANCE_VISION = "https://data.binance.vision"
BYBIT = "https://api.bybit.com"
OKX = "https://www.okx.com"


# ── canonical contract ─────────────────────────────────────────────────────
ALPHA_DATA_CONTRACT = {
    "version": "ALPHA_DATA_CONTRACT_V1",
    "record_fields": {
        "source": "provider id, e.g. KUCOIN_FUTURES_PUBLIC_REST / BINANCE_VISION_ARCHIVE",
        "venue": "trading venue the value describes (KUCOIN_FUTURES, BINANCE_USDM, ...)",
        "symbol": "canonical symbol (BTCUSDT form); venue symbol kept in provenance",
        "metric": "canonical metric id, e.g. funding_rate_settled, open_interest, taker_buy_volume",
        "event_ts": "ms UTC: the instant the value refers to (settlement time, snapshot time, bar close)",
        "observed_ts": "ms UTC: earliest instant a live runtime could have KNOWN the value; decisions at D may "
                       "use a record only if observed_ts <= D",
        "interval_start": "ms UTC start of the period the value summarizes (== event_ts for snapshots)",
        "interval_end": "ms UTC end of that period",
        "value": "float in canonical units; MISSING is null - never 0",
        "units": "e.g. rate_per_interval, contracts, base_asset, quote_usd, ratio",
        "quality_flags": "list: DUPLICATE, NON_MONOTONIC, GAP_BEFORE, OUT_OF_RANGE, STALE_RUN, CONSERVATIVE_LAG, ...",
        "provenance": {"endpoint": "path without host secrets", "venue_symbol": "str", "retrieved_at": "ms UTC",
                       "evidence": "VERIFIED|DOCUMENTED", "checksum": "provider checksum if any"},
        "raw_payload_hash": "sha256 of the canonical JSON of the raw row"},
    "rules": {"point_in_time": "only records with observed_ts <= decision_ts are visible",
              "missing": "absent data stays absent (null); no forward-fill beyond the documented validity",
              "dedup_key": "(source, venue, symbol, metric, event_ts)",
              "no_secrets": "no credentials, tokens or signed URLs in any record or artifact"}}
ALPHA_DATA_CONTRACT_SHA256 = hashlib.sha256(json.dumps(ALPHA_DATA_CONTRACT, sort_keys=True).encode()).hexdigest()


def raw_hash(row) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def record(*, source, venue, symbol, metric, event_ts, observed_ts, interval_start, interval_end, value, units,
           endpoint, venue_symbol, retrieved_at, evidence, raw, flags=(), checksum=None) -> dict:
    v = None if value is None or (isinstance(value, float) and not math.isfinite(value)) else float(value)
    return {"source": source, "venue": venue, "symbol": symbol, "metric": metric, "event_ts": int(event_ts),
            "observed_ts": int(observed_ts), "interval_start": int(interval_start), "interval_end": int(interval_end),
            "value": v, "units": units, "quality_flags": sorted(set(flags) | ({"MISSING"} if v is None else set())),
            "provenance": {"endpoint": endpoint, "venue_symbol": venue_symbol, "retrieved_at": int(retrieved_at),
                           "evidence": evidence, "checksum": checksum},
            "raw_payload_hash": raw_hash(raw)}


def visible(records, decision_ts: int) -> list:
    """Point-in-time filter: the ONLY way a consumer may read contract records."""
    return [r for r in records if r["observed_ts"] <= int(decision_ts)]


class LookaheadRejected(ValueError):
    pass


def assert_point_in_time(records, decision_ts: int):
    bad = [r for r in records if r["observed_ts"] > int(decision_ts)]
    if bad:
        raise LookaheadRejected(f"{len(bad)} records not observable at {decision_ts}")


# ── normalization (shared by replay and a future runtime adapter) ──────────
def to_ms(ts) -> int:
    """Normalize provider timestamps (s, ms, us, ns, ISO) to UTC ms."""
    if isinstance(ts, str) and not ts.strip().lstrip("-").isdigit():
        d = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return int(d.timestamp() * 1000)
    t = int(float(ts))
    if t < 10 ** 11:
        return t * 1000
    if t < 10 ** 14:
        return t
    if t < 10 ** 17:
        return t // 1000
    return t // 1_000_000


_KU_ALIAS = {"BTC": "XBT"}


def kucoin_symbol(sym: str) -> str:
    base = sym[:-4] if sym.endswith("USDT") else sym
    return f"{_KU_ALIAS.get(base, base)}USDTM"


def canonical_symbol(venue_symbol: str) -> str:
    s = venue_symbol.upper().replace("-", "").replace("_", "").replace("SWAP", "")
    if s.endswith("USDTM"):
        s = s[:-1]
    if s.startswith("XBT"):
        s = "BTC" + s[3:]
    return s


def norm_kucoin_funding(row: dict, *, symbol: str, granularity_ms: int, retrieved_at: int) -> dict:
    tp = to_ms(row["timepoint"])
    return record(source="KUCOIN_FUTURES_PUBLIC_REST", venue="KUCOIN_FUTURES", symbol=symbol,
                  metric="funding_rate_settled", event_ts=tp, observed_ts=tp,
                  interval_start=tp - granularity_ms, interval_end=tp, value=_f(row.get("fundingRate")),
                  units="rate_per_funding_interval", endpoint="/api/v1/contract/funding-rates",
                  venue_symbol=kucoin_symbol(symbol), retrieved_at=retrieved_at, evidence="VERIFIED", raw=row)


def norm_kline_bar(ts_open, close, *, symbol, metric, source, venue, endpoint, venue_symbol, bar_ms, retrieved_at,
                   raw, units="price") -> dict:
    t = to_ms(ts_open)
    return record(source=source, venue=venue, symbol=symbol, metric=metric, event_ts=t + bar_ms,
                  observed_ts=t + bar_ms, interval_start=t, interval_end=t + bar_ms, value=_f(close), units=units,
                  endpoint=endpoint, venue_symbol=venue_symbol, retrieved_at=retrieved_at, evidence="VERIFIED", raw=raw)


CONSERVATIVE_SNAPSHOT_LAG_MS = MIN5 + 10 * 60_000   # one 5m interval + 10 min publication allowance


def norm_binance_metric(row: dict, *, metric: str, retrieved_at: int, checksum=None) -> dict:
    t = to_ms(row["create_time"])
    return record(source="BINANCE_VISION_ARCHIVE", venue="BINANCE_USDM", symbol=row["symbol"], metric=metric,
                  event_ts=t, observed_ts=t + CONSERVATIVE_SNAPSHOT_LAG_MS, interval_start=t - MIN5, interval_end=t,
                  value=_f(row.get(metric)), units=METRIC_UNITS.get(metric, "unknown"),
                  endpoint="/data/futures/um/daily/metrics", venue_symbol=row["symbol"], retrieved_at=retrieved_at,
                  evidence="VERIFIED", raw=row, flags=("CONSERVATIVE_LAG",), checksum=checksum)


METRIC_UNITS = {"sum_open_interest": "base_asset", "sum_open_interest_value": "quote_usd",
                "count_toptrader_long_short_ratio": "ratio", "sum_toptrader_long_short_ratio": "ratio",
                "count_long_short_ratio": "ratio", "sum_taker_long_short_vol_ratio": "ratio"}


def _f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


# ── series quality ─────────────────────────────────────────────────────────
def series_quality(ts, values=None, *, step_ms: int | None, start: int, end: int, lo=None, hi=None,
                   stale_run=24) -> dict:
    """Coverage / gaps / duplicates / monotonicity / range / stale runs of one series."""
    ts = [int(t) for t in ts]
    n = len(ts)
    dup = n - len(set(ts))
    nonmono = sum(1 for a, b in zip(ts, ts[1:]) if b <= a)
    inside = sorted(set(t for t in ts if start <= t < end))
    out = {"rows": n, "duplicates": dup, "non_monotonic": nonmono, "first_ts": ts[0] if ts else None,
           "last_ts": ts[-1] if ts else None, "rows_in_window": len(inside)}
    if step_ms:
        expected = max(1, (end - start) // step_ms)
        gaps = [(b - a) // step_ms - 1 for a, b in zip(inside, inside[1:]) if b - a > step_ms]
        lead = ((inside[0] - start) // step_ms) if inside else expected
        tail = ((end - step_ms - inside[-1]) // step_ms) if inside else expected
        off = Counter((t - start) % step_ms for t in inside)
        out.update({"expected": expected, "coverage": min(1.0, len(inside) / expected), "gap_count": len(gaps),
                    "max_gap_intervals": max(gaps + [max(lead, 0), max(tail, 0)]) if inside else expected,
                    "leading_missing_intervals": max(lead, 0), "trailing_missing_intervals": max(tail, 0),
                    "timestamp_offset_errors": sum(c for o, c in off.items() if o != 0)})
    if values is not None:
        v = np.array([np.nan if x is None else float(x) for x in values], float)
        fin = v[np.isfinite(v)]
        out["missing_values"] = int((~np.isfinite(v)).sum())
        out["impossible_values"] = int(((fin < lo) if lo is not None else np.zeros(len(fin), bool)).sum()
                                       + ((fin > hi) if hi is not None else np.zeros(len(fin), bool)).sum())
        run, best = 1, 1
        for a, b in zip(fin, fin[1:]):
            run = run + 1 if a == b else 1
            best = max(best, run)
        out["longest_constant_run"] = int(best) if len(fin) else 0
        out["stale_flag"] = bool(len(fin) and best >= stale_run)
        if len(fin) > 2:
            d = np.abs(np.diff(fin))
            md = float(np.median(d)) or 1e-12
            out["discontinuities_gt_50x_median_step"] = int((d > 50 * md).sum())
    return out


# ── HTTP ───────────────────────────────────────────────────────────────────
class Http:
    """Public GET only. Never sends credentials; records every status."""

    def __init__(self, concurrency=6):
        self.log = []
        self.sem = defaultdict(lambda: asyncio.Semaphore(concurrency))
        self.session = None

    async def __aenter__(self):
        import aiohttp
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40),
                                             headers={"User-Agent": "nexus7-research-audit/1.0"})
        return self

    async def __aexit__(self, *a):
        await self.session.close()

    async def get(self, url, params=None, *, raw=False, retries=2):
        host = url.split("/")[2]
        async with self.sem[host]:
            for attempt in range(retries + 1):
                t0 = time.time()
                try:
                    async with self.session.get(url, params=params or {}) as r:
                        body = await r.read()
                        status = r.status
                        hdr = {k: v for k, v in r.headers.items() if k.lower().startswith(("x-mbx-used-weight", "retry-after", "x-ratelimit", "gw-ratelimit"))}
                except Exception as exc:
                    self.log.append({"host": host, "path": url.split(host, 1)[1].split("?")[0], "status": None,
                                     "error": type(exc).__name__})
                    if attempt < retries:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    return None, None, {}
                self.log.append({"host": host, "path": url.split(host, 1)[1].split("?")[0], "status": status,
                                 "bytes": len(body), "ms": int((time.time() - t0) * 1000)})
                if status in (429, 500, 502, 503, 504) and attempt < retries:
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
                if raw:
                    return status, body, hdr
                try:
                    return status, json.loads(body), hdr
                except Exception:
                    return status, None, hdr
        return None, None, {}


def _shape(obj, depth=0):
    if depth > 2:
        return type(obj).__name__
    if isinstance(obj, dict):
        return {k: _shape(v, depth + 1) for k, v in list(obj.items())[:25]}
    if isinstance(obj, list):
        return [_shape(obj[0], depth + 1)] if obj else []
    return type(obj).__name__


def _ku_data(js):
    if isinstance(js, dict) and str(js.get("code")) == "200000":
        return js.get("data"), None
    return None, (js.get("code"), js.get("msg")) if isinstance(js, dict) else ("NO_JSON", None)


# ── KuCoin probes ──────────────────────────────────────────────────────────
async def kucoin_contracts(http) -> dict:
    st, js, _ = await http.get(f"{KUCOIN}/api/v1/contracts/active")
    data, err = _ku_data(js) if js is not None else (None, ("NO_RESPONSE", None))
    out = {"status": st, "error": err, "symbols": {}}
    if isinstance(data, list):
        by = {c.get("symbol"): c for c in data if isinstance(c, dict)}
        for s in SYMBOLS:
            c = by.get(kucoin_symbol(s))
            if c:
                out["symbols"][s] = {k: c.get(k) for k in (
                    "symbol", "indexSymbol", "premiumsSymbol1M", "premiumsSymbol8H", "fundingBaseSymbol",
                    "fundingQuoteSymbol", "fundingRateSymbol", "fundingRateGranularity", "openInterest",
                    "firstOpenDate", "markMethod", "fairMethod", "multiplier", "status", "currentFundingRate",
                    "predictedFundingRate", "turnoverOf24h", "volumeOf24h")}
        out["fields_available"] = sorted(set().union(*[set(c.keys()) for c in data if isinstance(c, dict)])) if data else []
    return out


async def kucoin_funding_history(http, sym: str, start: int, end: int) -> tuple[list, dict]:
    rows, pages, maxret, errors = {}, 0, 0, []
    cur, chunk = start, 7 * DAY
    while cur < end:
        to = min(end, cur + chunk)
        st, js, _ = await http.get(f"{KUCOIN}/api/v1/contract/funding-rates",
                                   {"symbol": kucoin_symbol(sym), "from": str(cur), "to": str(to)})
        pages += 1
        data, err = _ku_data(js) if js is not None else (None, ("NO_RESPONSE", st))
        if err:
            errors.append({"from": cur, "status": st, "error": str(err)[:120]})
        if isinstance(data, list):
            maxret = max(maxret, len(data))
            for r in data:
                if isinstance(r, dict) and r.get("timepoint") is not None:
                    rows[to_ms(r["timepoint"])] = r
        cur = to
    return [rows[k] for k in sorted(rows)], {"pages": pages, "max_rows_per_page": maxret, "errors": errors[:5],
                                              "error_count": len(errors),
                                              "possible_truncation": maxret in (100, 200, 500, 1000)}


async def kucoin_earliest_funding(http, sym: str) -> dict:
    res = {}
    for days in DEPTH_PROBE_DAYS:
        a = WINDOW_END_MS - days * DAY
        st, js, _ = await http.get(f"{KUCOIN}/api/v1/contract/funding-rates",
                                   {"symbol": kucoin_symbol(sym), "from": str(a), "to": str(a + 3 * DAY)})
        data, err = _ku_data(js) if js is not None else (None, ("NO_RESPONSE", st))
        res[str(days)] = {"status": st, "rows": len(data) if isinstance(data, list) else None,
                          "error": str(err)[:80] if err else None}
    return res


async def kucoin_klines(http, venue_symbol: str, start: int, end: int, *, page_bars=150) -> tuple[list, dict]:
    cur, by, pages, maxret, errs = start, {}, 0, 0, 0
    while cur < end:
        to = min(end, cur + page_bars * H1)
        st, js, _ = await http.get(f"{KUCOIN}/api/v1/kline/query",
                                   {"symbol": venue_symbol, "granularity": "60", "from": str(cur), "to": str(to)})
        pages += 1
        data, err = _ku_data(js) if js is not None else (None, ("NO_RESPONSE", st))
        if err:
            errs += 1
            if errs >= 3 and not by:
                break
        if isinstance(data, list):
            maxret = max(maxret, len(data))
            for k in data:
                try:
                    t = to_ms(k[0])
                    if cur <= t <= to:
                        by[t] = [t, float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]) if len(k) > 5 else None]
                except Exception:
                    continue
        cur = to
    return [by[t] for t in sorted(by)], {"pages": pages, "max_rows_per_page": maxret, "error_pages": errs}


async def kucoin_history_probe(http, path: str, venue_symbol: str) -> dict:
    """index/premium/interest query endpoints: depth probe with small windows."""
    res = {}
    for days in DEPTH_PROBE_DAYS:
        a = WINDOW_END_MS - days * DAY
        st, js, _ = await http.get(f"{KUCOIN}{path}", {"symbol": venue_symbol, "startAt": str(a),
                                                        "endAt": str(a + 2 * H1), "maxCount": "100", "reverse": "false",
                                                        "forward": "true"})
        data, err = _ku_data(js) if js is not None else (None, ("NO_RESPONSE", st))
        lst = data.get("dataList") if isinstance(data, dict) else data if isinstance(data, list) else None
        first = None
        if lst:
            first = lst[0]
        res[str(days)] = {"status": st, "rows": len(lst) if isinstance(lst, list) else None,
                          "error": str(err)[:80] if err else None,
                          "first_ts": to_ms(first.get("timePoint")) if isinstance(first, dict) and first.get("timePoint") else None,
                          "sample_keys": sorted(first.keys()) if isinstance(first, dict) else None}
    return res


async def kucoin_live_snapshots(http, sym: str) -> dict:
    ks = kucoin_symbol(sym)
    out = {}
    for name, path in (("funding_current", f"/api/v1/funding-rate/{ks}/current"),
                       ("mark_current", f"/api/v1/mark-price/{ks}/current"),
                       ("contract_detail", f"/api/v1/contracts/{ks}"),
                       ("ticker", "/api/v1/ticker"), ("depth20", "/api/v1/level2/depth20"),
                       ("trade_history", "/api/v1/trade/history")):
        params = {"symbol": ks} if name in ("ticker", "depth20", "trade_history") else None
        st, js, hdr = await http.get(f"{KUCOIN}{path}", params)
        data, err = _ku_data(js) if js is not None else (None, ("NO_RESPONSE", st))
        o = {"status": st, "error": str(err)[:80] if err else None, "shape": _shape(data), "rate_limit_headers": hdr}
        if name == "trade_history" and isinstance(data, list) and data:
            ts = sorted(to_ms(t.get("ts")) for t in data if isinstance(t, dict) and t.get("ts"))
            o.update({"rows": len(data), "span_ms": (ts[-1] - ts[0]) if len(ts) > 1 else None,
                      "has_side": any("side" in t for t in data if isinstance(t, dict))})
        if name == "depth20" and isinstance(data, dict):
            o.update({"bids": len(data.get("bids") or []), "asks": len(data.get("asks") or []),
                      "sequence": data.get("sequence"), "ts": data.get("ts"), "bytes": len(json.dumps(data))})
        if name in ("funding_current", "mark_current", "contract_detail") and isinstance(data, dict):
            keep = ("value", "predictedValue", "timePoint", "granularity", "fundingRateCap", "fundingRateFloor",
                    "indexPrice", "openInterest", "fundingRateGranularity", "nextFundingRateTime",
                    "nextFundingRateDateTime", "currentFundingRate", "predictedFundingRate", "fundingFeeRate")
            o["fields"] = {k: data.get(k) for k in keep if k in data}
        out[name] = o
    # order-book update rate: two depth snapshots a few seconds apart
    s1 = out["depth20"].get("sequence")
    await asyncio.sleep(3.0)
    st, js, _ = await http.get(f"{KUCOIN}/api/v1/level2/depth20", {"symbol": ks})
    data, _ = _ku_data(js) if js is not None else (None, None)
    s2 = data.get("sequence") if isinstance(data, dict) else None
    try:
        out["depth20"]["sequence_updates_per_sec"] = (int(s2) - int(s1)) / 3.0
    except Exception:
        out["depth20"]["sequence_updates_per_sec"] = None
    return out


async def kucoin_candidate_paths(http) -> dict:
    """Paths that WOULD provide OI / liquidation / long-short history if they existed (record the answer)."""
    ks = kucoin_symbol("BTCUSDT")
    cands = {"open_interest_history_a": ("/api/v1/openInterest", {"symbol": ks}),
             "open_interest_history_b": ("/api/v2/open-interest", {"symbol": ks}),
             "liquidation_orders": ("/api/v1/liquidation/orders", {"symbol": ks}),
             "long_short_ratio": ("/api/v1/contract/long-short-ratio", {"symbol": ks}),
             "interest_rate_history": ("/api/v1/interest/query", {"symbol": ".USDTINT8H", "maxCount": "10"})}
    out = {}
    for k, (p, prm) in cands.items():
        st, js, _ = await http.get(f"{KUCOIN}{p}", prm)
        data, err = _ku_data(js) if js is not None else (None, ("NO_RESPONSE", st))
        out[k] = {"path": p, "status": st, "error": str(err)[:80] if err else None, "shape": _shape(data)}
    return out


# ── Binance (vision archive + REST reachability) ───────────────────────────
def _months(start, end):
    d = dt.datetime.fromtimestamp(start / 1000, dt.timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    e = dt.datetime.fromtimestamp(end / 1000, dt.timezone.utc)
    while d <= e:
        yield d.strftime("%Y-%m")
        d = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)


def _days(start, end):
    d = dt.datetime.fromtimestamp(start / 1000, dt.timezone.utc).date()
    e = dt.datetime.fromtimestamp(end / 1000, dt.timezone.utc).date()
    while d <= e:
        yield d.isoformat()
        d += dt.timedelta(days=1)


async def vision_file(http, path: str) -> tuple[list | None, dict]:
    """Download a data.binance.vision zip + .CHECKSUM, verify sha256, parse CSV."""
    st, body, _ = await http.get(f"{BINANCE_VISION}/{path}", raw=True, retries=1)
    meta = {"status": st}
    if st != 200 or not body:
        return None, meta
    sha = hashlib.sha256(body).hexdigest()
    st2, cks, _ = await http.get(f"{BINANCE_VISION}/{path}.CHECKSUM", raw=True, retries=1)
    exp = cks.decode(errors="ignore").split()[0] if st2 == 200 and cks else None
    meta.update({"sha256": sha, "checksum_expected": exp, "checksum_ok": exp == sha if exp else None,
                 "bytes": len(body)})
    try:
        z = zipfile.ZipFile(io.BytesIO(body))
        text = z.read(z.namelist()[0]).decode()
    except Exception as exc:
        meta["parse_error"] = type(exc).__name__
        return None, meta
    rows = list(csv.reader(io.StringIO(text)))
    return rows, meta


async def vision_dataset(http, kind: str, sym: str, start: int, end: int) -> tuple[list, dict]:
    """kind: metrics (daily only) | klines_1h | premium_1h | mark_1h | index_1h | funding (monthly+daily)."""
    rows, files = [], []
    if kind == "metrics":
        paths = [f"data/futures/um/daily/metrics/{sym}/{sym}-metrics-{d}.zip" for d in _days(start, end)]
        res = await asyncio.gather(*[vision_file(http, p) for p in paths])
        for p, (r, m) in zip(paths, res):
            files.append({"path": p, **m})
            if r:
                rows += r
        return rows, {"files": len(paths), "files_ok": sum(1 for f in files if f["status"] == 200),
                      "checksum_ok": sum(1 for f in files if f.get("checksum_ok")),
                      "checksum_bad": sum(1 for f in files if f.get("checksum_ok") is False),
                      "bytes": sum(f.get("bytes", 0) for f in files), "status_counts": dict(Counter(f["status"] for f in files))}
    sub = {"klines_1h": ("klines", "1h"), "premium_1h": ("premiumIndexKlines", "1h"), "mark_1h": ("markPriceKlines", "1h"),
           "index_1h": ("indexPriceKlines", "1h"), "funding": ("fundingRate", None)}[kind]
    for m in _months(start, end):
        mp = (f"data/futures/um/monthly/{sub[0]}/{sym}/{sub[1]}/{sym}-{sub[1]}-{m}.zip" if sub[1]
              else f"data/futures/um/monthly/{sub[0]}/{sym}/{sym}-{sub[0]}-{m}.zip")
        r, meta = await vision_file(http, mp)
        files.append({"path": mp, **meta})
        if r:
            rows += r
            continue
        if sub[1]:                                            # month not yet archived -> daily files
            y, mo = m.split("-")
            ds = [d for d in _days(start, end) if d.startswith(f"{y}-{mo}")]
            dps = [f"data/futures/um/daily/{sub[0]}/{sym}/{sub[1]}/{sym}-{sub[1]}-{d}.zip" for d in ds]
            res = await asyncio.gather(*[vision_file(http, p) for p in dps])
            for p, (rr, mm) in zip(dps, res):
                files.append({"path": p, **mm})
                if rr:
                    rows += rr
    return rows, {"files": len(files), "files_ok": sum(1 for f in files if f["status"] == 200),
                  "checksum_ok": sum(1 for f in files if f.get("checksum_ok")),
                  "checksum_bad": sum(1 for f in files if f.get("checksum_ok") is False),
                  "bytes": sum(f.get("bytes", 0) for f in files), "status_counts": dict(Counter(f["status"] for f in files))}


def parse_vision_klines(rows) -> list:
    out = []
    for r in rows:
        if not r or not r[0].strip().lstrip("-").isdigit():
            continue                                            # header rows
        try:
            out.append([to_ms(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]),
                        float(r[9]) if len(r) > 9 and r[9] != "" else None, float(r[7]) if len(r) > 7 and r[7] != "" else None])
        except (ValueError, IndexError):
            continue
    by = {}
    for x in out:
        by.setdefault(x[0], x)
    return [by[t] for t in sorted(by)]


def parse_vision_metrics(rows) -> list:
    out, header = [], None
    for r in rows:
        if r and r[0] == "create_time":
            header = r
            continue
        if not r:
            continue
        h = header or ["create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
                       "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio", "count_long_short_ratio",
                       "sum_taker_long_short_vol_ratio"]
        out.append(dict(zip(h, r)))
    return out


def parse_vision_funding(rows) -> list:
    out = []
    for r in rows:
        if not r or not r[0].strip().isdigit():
            continue
        try:
            out.append([to_ms(r[0]), float(r[2])])
        except (ValueError, IndexError):
            continue
    by = {t: v for t, v in out}
    return [[t, by[t]] for t in sorted(by)]


async def rest_probe(http, url, params) -> dict:
    st, js, hdr = await http.get(url, params, retries=1)
    rows = js if isinstance(js, list) else (js.get("result", {}).get("list") if isinstance(js, dict) and isinstance(js.get("result"), dict)
                                            else js.get("data") if isinstance(js, dict) else None)
    return {"status": st, "rows": len(rows) if isinstance(rows, list) else None, "shape": _shape(rows[:1] if isinstance(rows, list) else js),
            "error": (js.get("msg") or js.get("retMsg")) if isinstance(js, dict) and st != 200 else None,
            "rate_limit_headers": hdr}


async def third_party_rest(http) -> dict:
    now = WINDOW_END_MS
    old = now - 548 * DAY
    p = {}
    p["binance_funding_rate"] = await rest_probe(http, f"{BINANCE_FAPI}/fapi/v1/fundingRate",
                                                 {"symbol": "BTCUSDT", "startTime": old, "limit": 10})
    p["binance_oi_hist_recent"] = await rest_probe(http, f"{BINANCE_FAPI}/futures/data/openInterestHist",
                                                   {"symbol": "BTCUSDT", "period": "1h", "limit": 10})
    p["binance_oi_hist_548d"] = await rest_probe(http, f"{BINANCE_FAPI}/futures/data/openInterestHist",
                                                 {"symbol": "BTCUSDT", "period": "1h", "startTime": old, "limit": 10})
    p["binance_global_ls"] = await rest_probe(http, f"{BINANCE_FAPI}/futures/data/globalLongShortAccountRatio",
                                              {"symbol": "BTCUSDT", "period": "1h", "limit": 10})
    p["binance_taker_ls"] = await rest_probe(http, f"{BINANCE_FAPI}/futures/data/takerlongshortRatio",
                                             {"symbol": "BTCUSDT", "period": "1h", "limit": 10})
    p["binance_force_orders"] = await rest_probe(http, f"{BINANCE_FAPI}/fapi/v1/allForceOrders", {"symbol": "BTCUSDT"})
    p["bybit_open_interest_548d"] = await rest_probe(http, f"{BYBIT}/v5/market/open-interest",
                                                     {"category": "linear", "symbol": "BTCUSDT", "intervalTime": "1h",
                                                      "startTime": old, "endTime": old + 10 * H1, "limit": 10})
    p["bybit_funding_548d"] = await rest_probe(http, f"{BYBIT}/v5/market/funding/history",
                                               {"category": "linear", "symbol": "BTCUSDT", "startTime": old,
                                                "endTime": old + 3 * DAY, "limit": 10})
    p["bybit_account_ratio"] = await rest_probe(http, f"{BYBIT}/v5/market/account-ratio",
                                                {"category": "linear", "symbol": "BTCUSDT", "period": "1h", "limit": 10})
    p["okx_funding_history"] = await rest_probe(http, f"{OKX}/api/v5/public/funding-rate-history",
                                                {"instId": "BTC-USDT-SWAP", "limit": "100"})
    p["okx_oi_history"] = await rest_probe(http, f"{OKX}/api/v5/rubik/stat/contracts/open-interest-history",
                                           {"instId": "BTC-USDT-SWAP", "period": "1H"})
    liq_day = dt.datetime.fromtimestamp((now - 60 * DAY) / 1000, dt.timezone.utc).date().isoformat()
    _, meta = await vision_file(http, f"data/futures/um/daily/liquidationSnapshot/BTCUSDT/BTCUSDT-liquidationSnapshot-{liq_day}.zip")
    p["binance_vision_liquidation_snapshot_60d_ago"] = meta
    return p


# ── orchestration (CI) ─────────────────────────────────────────────────────
def load_price_dataset(path):
    from bot.ai import long_horizon as lh
    return lh.load_dataset(path)


def manifest(name, *, source, metric_set, symbols, rows_by_symbol: dict, canonical, retrieved_at, extra=None) -> dict:
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    allts = [r[0] for v in rows_by_symbol.values() for r in v]
    return {"dataset": name, "source": source, "metrics": metric_set, "symbols": sorted(symbols),
            "rows": sum(len(v) for v in rows_by_symbol.values()),
            "first_ts": min(allts) if allts else None, "last_ts": max(allts) if allts else None,
            "retrieved_at": retrieved_at, "sha256": hashlib.sha256(blob).hexdigest(), "bytes_canonical": len(blob),
            **(extra or {})}


def _days_between(a, b):
    return (b - a) / DAY if a and b else 0.0


def corr(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 10 or np.std(a[ok]) == 0 or np.std(b[ok]) == 0:
        return None
    return float(np.corrcoef(a[ok], b[ok])[0, 1])


async def audit(price_path: str | None, out_dir: str) -> dict:
    from pathlib import Path
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    retrieved_at = int(time.time() * 1000)
    S, E = WINDOW_START_MS, WINDOW_END_MS
    res = {"retrieved_at": retrieved_at, "window": [S, E]}
    manifests = []
    async with Http() as http:
        res["kucoin_contracts"] = await kucoin_contracts(http)
        cmap = res["kucoin_contracts"]["symbols"]
        # funding history (full window) + depth probe
        fund, fund_meta, fund_depth = {}, {}, {}
        for s in SYMBOLS:
            rows, meta = await kucoin_funding_history(http, s, S, E)
            fund[s], fund_meta[s] = rows, meta
        fund_depth["BTCUSDT"] = await kucoin_earliest_funding(http, "BTCUSDT")
        fund_depth["ATOMUSDT"] = await kucoin_earliest_funding(http, "ATOMUSDT")
        res["kucoin_funding_meta"], res["kucoin_funding_depth"] = fund_meta, fund_depth
        # index klines (basis) - full window if the symbol serves klines
        idx, idx_meta = {}, {}
        for s in SYMBOLS:
            isym = (cmap.get(s) or {}).get("indexSymbol")
            if not isym:
                idx_meta[s] = {"error": "NO_INDEX_SYMBOL"}
                continue
            bars, meta = await kucoin_klines(http, isym, S, E)
            idx[s], idx_meta[s] = bars, {**meta, "index_symbol": isym}
        res["kucoin_index_kline_meta"] = idx_meta
        b = cmap.get("BTCUSDT") or {}
        res["kucoin_index_query_probe"] = await kucoin_history_probe(http, "/api/v1/index/query", b.get("indexSymbol") or ".KXBTUSDT")
        res["kucoin_premium_query_probe"] = await kucoin_history_probe(http, "/api/v1/premium/query",
                                                                       b.get("premiumsSymbol8H") or ".XBTUSDTMPI8H")
        res["kucoin_live"] = {s: await kucoin_live_snapshots(http, s) for s in ("BTCUSDT", "DOGEUSDT")}
        res["kucoin_candidate_paths"] = await kucoin_candidate_paths(http)
        res["third_party_rest"] = await third_party_rest(http)
        # Binance vision archives (cross-source proxies)
        vis = {}
        for kind in ("funding", "klines_1h", "premium_1h", "metrics"):
            vis[kind] = {}
            for s in SYMBOLS:
                rows, meta = await vision_dataset(http, kind, s, S, E)
                vis[kind][s] = (rows, meta)
        res["http_log_summary"] = {
            "requests": len(http.log),
            "by_host_status": {h: dict(Counter(str(x["status"]) for x in http.log if x["host"] == h))
                               for h in sorted({x["host"] for x in http.log})}}
    # ── parse / quality / manifests ──
    q = {}
    # KuCoin funding
    kf = {}
    for s in SYMBOLS:
        rows = fund[s]
        gran = int((cmap.get(s) or {}).get("fundingRateGranularity") or H8)
        recs = [norm_kucoin_funding(r, symbol=s, granularity_ms=gran, retrieved_at=retrieved_at) for r in rows]
        ts = [r["event_ts"] for r in recs]
        steps = Counter(b - a for a, b in zip(ts, ts[1:]))
        modal = steps.most_common(1)[0][0] if steps else H8
        qs = series_quality(ts, [r["value"] for r in recs], step_ms=None, start=S, end=E, lo=-0.05, hi=0.05)
        exp_8h = (E - S) // H8
        gaps = sum((b - a) // H8 - 1 for a, b in zip(ts, ts[1:]) if b - a > H8)
        qs.update({"interval_histogram_h": {str(k // H1): v for k, v in steps.most_common(6)},
                   "modal_interval_h": modal / H1, "expected_8h_settlements": exp_8h,
                   "coverage_vs_8h_grid": min(1.0, len({t - t % H8 for t in ts if S <= t < E}) / exp_8h),
                   "missing_8h_slots": gaps, "future_dated_records": sum(1 for t in ts if t > retrieved_at),
                   "offgrid_timestamps": sum(1 for t in ts if t % H1 != 0)})
        q.setdefault("kucoin_funding", {})[s] = qs
        kf[s] = [[r["event_ts"], r["value"]] for r in recs]
    manifests.append(manifest("kucoin_funding_history", source="KUCOIN_FUTURES_PUBLIC_REST",
                              metric_set=["funding_rate_settled"], symbols=SYMBOLS, rows_by_symbol=kf, canonical=kf,
                              retrieved_at=retrieved_at))
    # KuCoin index klines + basis vs perp close (price dataset)
    price = None
    if price_path:
        try:
            price, hdr = load_price_dataset(price_path)
            res["price_dataset_sha256"] = hdr["sha256"]
        except Exception as exc:
            res["price_dataset_error"] = type(exc).__name__
    ki = {s: [[b[0], b[4]] for b in idx.get(s, [])] for s in SYMBOLS}
    for s in SYMBOLS:
        bars = idx.get(s, [])
        q.setdefault("kucoin_index_kline", {})[s] = series_quality([b[0] for b in bars], [b[4] for b in bars],
                                                                   step_ms=H1, start=S, end=E, lo=1e-12)
    manifests.append(manifest("kucoin_index_kline_1h", source="KUCOIN_FUTURES_PUBLIC_REST", metric_set=["index_close"],
                              symbols=[s for s in SYMBOLS if idx.get(s)], rows_by_symbol=ki, canonical=ki,
                              retrieved_at=retrieved_at))
    basis = {}
    if price:
        for s in SYMBOLS:
            pc = {int(b["ts"]): float(b["c"]) for b in price.get(s, [])}
            pairs = [(t, pc[t], c) for t, c in ki[s] if t in pc and c]
            if pairs:
                bp = np.array([(p - i) / i for _, p, i in pairs])
                basis[s] = {"aligned_hours": len(pairs), "median_basis_pct": float(np.median(bp) * 100),
                            "p01_pct": float(np.quantile(bp, .01) * 100), "p99_pct": float(np.quantile(bp, .99) * 100),
                            "abs_gt_2pct_hours": int((np.abs(bp) > 0.02).sum())}
    q["kucoin_basis_from_index_kline"] = basis
    # Binance vision
    bv = {}
    for kind, per in vis.items():
        rows_by = {}
        for s, (rows, meta) in per.items():
            if kind == "metrics":
                recs = parse_vision_metrics(rows)
                ts = [to_ms(r["create_time"]) for r in recs if r.get("create_time")]
                vals = [_f(r.get("sum_open_interest")) for r in recs]
                qs = series_quality(ts, vals, step_ms=MIN5, start=S, end=E, lo=0)
                qs["files"] = meta
                rows_by[s] = [[t, v] for t, v in zip(ts, vals)]
            elif kind == "funding":
                fr = parse_vision_funding(rows)
                qs = series_quality([r[0] for r in fr], [r[1] for r in fr], step_ms=None, start=S, end=E, lo=-0.05, hi=0.05)
                qs["files"] = meta
                rows_by[s] = fr
            else:
                kl = parse_vision_klines(rows)
                qs = series_quality([r[0] for r in kl], [r[4] for r in kl], step_ms=H1, start=S, end=E)
                if kind == "klines_1h":
                    tb = [r[6] for r in kl]
                    qs["taker_buy_volume_present"] = sum(1 for x in tb if x is not None)
                    qs["taker_buy_gt_volume"] = sum(1 for r in kl if r[6] is not None and r[6] > r[5] * (1 + 1e-9))
                qs["files"] = meta
                rows_by[s] = [[r[0], r[4]] + ([r[6], r[5]] if kind == "klines_1h" else []) for r in kl]
            q.setdefault(f"binance_vision_{kind}", {})[s] = qs
        bv[kind] = rows_by
        manifests.append(manifest(f"binance_vision_{kind}", source="BINANCE_VISION_ARCHIVE", metric_set=[kind],
                                  symbols=[s for s, v in rows_by.items() if v], rows_by_symbol=rows_by,
                                  canonical=rows_by, retrieved_at=retrieved_at))
    # cross-source consistency (series vs series; NOT vs outcomes)
    cs = {"funding_kucoin_vs_binance": {}, "perp_close_kucoin_vs_binance": {}, "basis_kucoin_index_vs_binance_premium": {}}
    for s in SYMBOLS:
        a = {t: v for t, v in kf[s] if v is not None}
        bfd = {t: v for t, v in bv["funding"].get(s, [])}
        common = sorted(set(a) & set(bfd))
        if a and bfd:
            near = sum(1 for t in a if any(abs(t - u) <= 60_000 for u in (t,)) and t in bfd)
            cs["funding_kucoin_vs_binance"][s] = {
                "kucoin_rows": len(a), "binance_rows": len(bfd), "exact_timestamp_matches": len(common),
                "match_fraction_of_kucoin": near / len(a) if a else None,
                "corr": corr([a[t] for t in common], [bfd[t] for t in common]),
                "median_abs_diff": float(np.median([abs(a[t] - bfd[t]) for t in common])) if common else None,
                "scale_ratio_median_abs": (float(np.median([abs(a[t]) for t in common])) /
                                           max(1e-12, float(np.median([abs(bfd[t]) for t in common])))) if common else None}
        if price:
            pc = {int(b["ts"]): float(b["c"]) for b in price.get(s, [])}
            bk = {r[0]: r[1] for r in bv["klines_1h"].get(s, [])}
            common = sorted(set(pc) & set(bk))
            if len(common) > 50:
                ka = np.array([pc[t] for t in common])
                kb = np.array([bk[t] for t in common])
                la, lb = np.diff(np.log(ka)), np.diff(np.log(kb))
                lags = {}
                for lag in (-2, -1, 0, 1, 2):
                    if lag >= 0:
                        lags[str(lag)] = corr(la[lag:], lb[:len(lb) - lag] if lag else lb)
                    else:
                        lags[str(lag)] = corr(la[:lag], lb[-lag:])
                cs["perp_close_kucoin_vs_binance"][s] = {
                    "aligned_hours": len(common), "median_abs_rel_diff": float(np.median(np.abs(ka / kb - 1))),
                    "return_corr_by_lag_h": lags, "best_lag_h": max(lags, key=lambda k: lags[k] or -1)}
            bp = {r[0]: r[1] for r in bv["premium_1h"].get(s, [])}
            ix = {t: c for t, c in ki[s]}
            common = sorted(set(pc) & set(ix) & set(bp))
            if len(common) > 50:
                kb_ = np.array([(pc[t] - ix[t]) / ix[t] for t in common])
                bb_ = np.array([bp[t] for t in common])
                cs["basis_kucoin_index_vs_binance_premium"][s] = {"aligned_hours": len(common), "corr": corr(kb_, bb_),
                                                                  "median_kucoin": float(np.median(kb_)),
                                                                  "median_binance": float(np.median(bb_))}
    # cross-sectional feature contract
    xsec = None
    if price:
        from bot.ai import xsec_features as xf
        sub = {s: [b for b in v if S - 32 * DAY <= int(b["ts"]) < E] for s, v in price.items()}
        par = xf.parity_report(sub, samples=150)
        M = xf.matrix(sub, xf.grid(sub)[::4])
        xsec = {"parity": par, "quality": xf.quality(M), "features_sha256": xf.features_sha256(M),
                "decisions_evaluated": int(len(M["T"]))}
    res.update({"quality": q, "cross_source": cs, "manifests": manifests, "xsec": xsec,
                "canonical_datasets": {"kucoin_funding": kf, "kucoin_index_close": ki, "binance": bv}})
    return res


# ── classification / reports ───────────────────────────────────────────────
def summarize_quality(per_symbol: dict, key="coverage") -> dict:
    vals = [v.get(key) for v in per_symbol.values() if isinstance(v, dict) and v.get(key) is not None]
    return {"symbols": len(per_symbol), "symbols_with_data": sum(1 for v in per_symbol.values()
                                                                 if isinstance(v, dict) and v.get("rows_in_window")),
            f"min_{key}": min(vals) if vals else None, f"mean_{key}": float(np.mean(vals)) if vals else None,
            "max_gap_intervals": max((v.get("max_gap_intervals") or 0) for v in per_symbol.values()) if per_symbol else None,
            "duplicates": sum(v.get("duplicates", 0) for v in per_symbol.values()),
            "non_monotonic": sum(v.get("non_monotonic", 0) for v in per_symbol.values()),
            "impossible_values": sum(v.get("impossible_values", 0) for v in per_symbol.values()),
            "timestamp_offset_errors": sum(v.get("timestamp_offset_errors", 0) or 0 for v in per_symbol.values()),
            "stale_symbols": [s for s, v in per_symbol.items() if v.get("stale_flag")]}


def classify_source(src: dict) -> str:
    """Predeclared, outcome-free rules."""
    if src.get("availability") in ("UNAVAILABLE", "UNREACHABLE") or src["lookahead_risk"] == "FAIL":
        return "REJECTED"
    days = src.get("history_days") or 0
    cov = src.get("coverage")
    if days < MIN_HISTORY_DAYS:
        return "PROSPECTIVE_ONLY" if src.get("live_endpoint_verified") else "REJECTED"
    if cov is None and src.get("probe_only"):
        return "NEEDS_ENGINEERING"                      # depth verified, full download + coverage still required
    if cov is None or cov < ENGINEERING_COVERAGE:
        return "REJECTED"
    if (src["parity"] == "PARITY_READY" and cov >= READY_COVERAGE and src["cost_class"] == "FREE_PUBLIC"
            and src.get("point_in_time") is True and src["lookahead_risk"] in ("PASS", "PASS_CONSERVATIVE_LAG")):
        return "READY_FOR_8G_B"
    return "NEEDS_ENGINEERING"
