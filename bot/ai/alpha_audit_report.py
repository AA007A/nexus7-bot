"""Phase 8G-A report builder: turns audit measurements into the 14 artifacts.

Outcome-free by construction: nothing here reads returns, PnL or labels.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from bot.ai import alpha_audit as aa

KU_RATE = "DOCUMENTED: KuCoin public REST is weight/rate-limited per IP (futures public pool); 429 on excess"
BN_RATE = "DOCUMENTED: data.binance.vision is a static archive (no API weight); fapi REST is weight-limited per IP"


def _days(first, last):
    return round((last - first) / aa.DAY, 1) if first and last else 0.0


def _span(per_symbol: dict):
    f = [v.get("first_ts") for v in per_symbol.values() if isinstance(v, dict) and v.get("first_ts")]
    l_ = [v.get("last_ts") for v in per_symbol.values() if isinstance(v, dict) and v.get("last_ts")]
    return (min(f) if f else None, max(l_) if l_ else None)


def _min_cov(per_symbol, key="coverage"):
    vals = [v.get(key) for v in per_symbol.values() if isinstance(v, dict) and v.get(key) is not None]
    return min(vals) if len(vals) == len(per_symbol) and vals else (min(vals) if vals else None)


def _iso(ms):
    import datetime as dt
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat() if ms else None


def build_sources(r: dict) -> list:
    q = r["quality"]
    tp = r["third_party_rest"]
    live = r["kucoin_live"]["BTCUSDT"]
    cand = r["kucoin_candidate_paths"]
    src = []

    def add(**k):
        k.setdefault("symbols_available", 0)
        k.setdefault("live_endpoint_verified", False)
        k.setdefault("point_in_time", None)
        k.setdefault("revision_behavior", "UNKNOWN: single retrieval; revision monitoring requires repeated snapshots")
        k["phase_8g_b_class"] = aa.classify_source(k)
        k["history_start"], k["history_end"] = _iso(k.get("first_ts")), _iso(k.get("last_ts"))
        src.append(k)

    # KuCoin funding (settled)
    kf = q["kucoin_funding"]
    f0, f1 = _span(kf)
    add(id="KUCOIN_FUNDING_SETTLED", family="FUNDING", provider="KuCoin", venue="KUCOIN_FUTURES",
        metric="funding_rate_settled", availability="VERIFIED" if f0 else "UNAVAILABLE", auth="NONE",
        cost_class="FREE_PUBLIC", historical_endpoint="GET /api/v1/contract/funding-rates?symbol&from&to",
        runtime_endpoint="GET /api/v1/contract/funding-rates (latest settled) ; GET /api/v1/funding-rate/{symbol}/current",
        first_ts=f0, last_ts=f1, history_days=_days(max(f0 or 0, aa.WINDOW_START_MS), f1) if f0 else 0,
        symbols_available=sum(1 for v in kf.values() if v.get("rows_in_window")),
        coverage=_min_cov(kf, "coverage_vs_8h_grid"), gaps=sum(v.get("missing_8h_slots", 0) for v in kf.values()),
        sampling_frequency="per settlement (modal interval h: "
                           f"{sorted({v.get('modal_interval_h') for v in kf.values()})})",
        pagination=f"time-window paging (7d windows); max rows per page observed "
                   f"{max(m.get('max_rows_per_page', 0) for m in r['kucoin_funding_meta'].values())}",
        rate_limit=KU_RATE, point_in_time=True,
        timestamp_semantics="timepoint = settlement instant; fundingRate = the rate settled at that instant; "
                            "interval = [timepoint - granularity, timepoint]; observed_ts = timepoint",
        lookahead_risk="PASS" if sum(v.get("future_dated_records", 0) for v in kf.values()) == 0 else "FAIL",
        lookahead_evidence="VERIFIED: no future-dated records; settled values are only used at/after timepoint",
        parity="PARITY_READY", parity_note="same venue and endpoint for history and runtime")
    # predicted funding
    fc = live.get("funding_current", {})
    add(id="KUCOIN_FUNDING_PREDICTED", family="FUNDING", provider="KuCoin", venue="KUCOIN_FUTURES",
        metric="funding_rate_predicted", availability="VERIFIED_LIVE_ONLY" if fc.get("status") == 200 else "UNREACHABLE",
        auth="NONE", cost_class="FREE_PUBLIC", historical_endpoint=None,
        runtime_endpoint="GET /api/v1/funding-rate/{symbol}/current (value/predictedValue)",
        live_endpoint_verified=fc.get("status") == 200, history_days=0, coverage=None,
        sampling_frequency="continuous (live snapshot)", pagination="n/a", rate_limit=KU_RATE, point_in_time=True,
        timestamp_semantics=f"live fields observed: {sorted((fc.get('fields') or {}).keys())}; no historical series",
        lookahead_risk="PASS", lookahead_evidence="live snapshot stamped at ingestion", parity="PARITY_POSSIBLE_WITH_IMPLEMENTATION",
        parity_note="history not reconstructable: prospective collection only", prospective_collection_required=True)
    # basis from index klines
    ik = q["kucoin_index_kline"]
    i0, i1 = _span(ik)
    ok_idx = any(v.get("rows_in_window") for v in ik.values())
    add(id="KUCOIN_BASIS_INDEX_KLINE", family="MARK_INDEX_BASIS", provider="KuCoin", venue="KUCOIN_FUTURES",
        metric="index_close_1h (basis = perp_close/index_close - 1)", availability="VERIFIED" if ok_idx else "UNAVAILABLE",
        auth="NONE", cost_class="FREE_PUBLIC",
        historical_endpoint="GET /api/v1/kline/query?symbol=<indexSymbol>&granularity=60&from&to",
        runtime_endpoint="same kline endpoint; GET /api/v1/mark-price/{symbol}/current (indexPrice)",
        first_ts=i0, last_ts=i1, history_days=_days(max(i0 or 0, aa.WINDOW_START_MS), i1) if i0 else 0,
        symbols_available=sum(1 for v in ik.values() if v.get("rows_in_window")), coverage=_min_cov(ik),
        gaps=sum(v.get("gap_count", 0) for v in ik.values()), sampling_frequency="1h",
        pagination="<=150 bars per request (exchange max 200)", rate_limit=KU_RATE, point_in_time=True,
        timestamp_semantics="kline ts = bar open; value known at bar close (ts + 1h)",
        lookahead_risk="PASS" if ok_idx else "FAIL", lookahead_evidence="bar-close observability, as the Phase 8F price parity",
        parity="PARITY_READY" if ok_idx else "REJECTED", parity_note="index and perp klines from the same venue endpoint")
    # premium / index query endpoints
    for pid, key, metric in (("KUCOIN_PREMIUM_INDEX_QUERY", "kucoin_premium_query_probe", "premium_index"),
                             ("KUCOIN_INDEX_QUERY", "kucoin_index_query_probe", "index_price_points")):
        pr = r[key]
        deep = [int(d) for d, v in pr.items() if (v.get("rows") or 0) > 0]
        add(id=pid, family="MARK_INDEX_BASIS", provider="KuCoin", venue="KUCOIN_FUTURES", metric=metric,
            availability="VERIFIED" if deep else "UNAVAILABLE", auth="NONE", cost_class="FREE_PUBLIC",
            historical_endpoint=f"GET {'/api/v1/premium/query' if 'PREMIUM' in pid else '/api/v1/index/query'}"
                                "?symbol&startAt&endAt&maxCount<=100",
            runtime_endpoint="same endpoint (latest points)", history_days=max(deep) if deep else 0,
            first_ts=None, last_ts=None, coverage=None, probe_only=True, depth_probe=pr, sampling_frequency="per published point (see sample)",
            pagination="maxCount <= 100 points per request (depth probed, not fully downloaded)",
            rate_limit=KU_RATE, point_in_time=True, live_endpoint_verified=bool(deep),
            timestamp_semantics="timePoint per published point; full-window coverage NOT measured (probe only)",
            lookahead_risk="PASS" if deep else "FAIL", lookahead_evidence="probe only",
            parity="PARITY_POSSIBLE_WITH_IMPLEMENTATION" if deep else "REJECTED",
            parity_note="needs a paging downloader (100 points/request) before 8G-B")
    # mark price
    mc = live.get("mark_current", {})
    add(id="KUCOIN_MARK_PRICE", family="MARK_INDEX_BASIS", provider="KuCoin", venue="KUCOIN_FUTURES", metric="mark_price",
        availability="VERIFIED_LIVE_ONLY" if mc.get("status") == 200 else "UNREACHABLE", auth="NONE",
        cost_class="FREE_PUBLIC", historical_endpoint=None, runtime_endpoint="GET /api/v1/mark-price/{symbol}/current",
        live_endpoint_verified=mc.get("status") == 200, history_days=0, coverage=None,
        sampling_frequency="live snapshot", pagination="n/a", rate_limit=KU_RATE, point_in_time=True,
        timestamp_semantics=f"fields: {sorted((mc.get('fields') or {}).keys())}", lookahead_risk="PASS",
        lookahead_evidence="live only", parity="PARITY_POSSIBLE_WITH_IMPLEMENTATION",
        parity_note="no public mark-price history verified", prospective_collection_required=True)
    # open interest (KuCoin)
    cd = live.get("contract_detail", {})
    add(id="KUCOIN_OPEN_INTEREST", family="OPEN_INTEREST", provider="KuCoin", venue="KUCOIN_FUTURES",
        metric="open_interest_snapshot",
        availability="VERIFIED_LIVE_ONLY" if (cd.get("fields") or {}).get("openInterest") is not None else "UNAVAILABLE",
        auth="NONE", cost_class="FREE_PUBLIC", historical_endpoint=None,
        runtime_endpoint="GET /api/v1/contracts/{symbol} (openInterest)",
        live_endpoint_verified=(cd.get("fields") or {}).get("openInterest") is not None, history_days=0, coverage=None,
        candidate_history_paths=cand, sampling_frequency="live snapshot", pagination="n/a", rate_limit=KU_RATE,
        point_in_time=True, timestamp_semantics="snapshot at request time (no exchange timestamp on the field)",
        lookahead_risk="PASS", lookahead_evidence="ingestion timestamp", parity="PARITY_POSSIBLE_WITH_IMPLEMENTATION",
        parity_note="no public OI history on KuCoin verified", prospective_collection_required=True)
    th = live.get("trade_history", {})
    add(id="KUCOIN_TAKER_FLOW", family="AGGRESSOR_TAKER_FLOW", provider="KuCoin", venue="KUCOIN_FUTURES",
        metric="signed trades (side = taker side)",
        availability="VERIFIED_LIVE_ONLY" if th.get("status") == 200 else "UNREACHABLE", auth="NONE",
        cost_class="FREE_PUBLIC", historical_endpoint=None,
        runtime_endpoint="REST /api/v1/trade/history (latest trades only); WebSocket /contractMarket/execution:{symbol}",
        live_endpoint_verified=th.get("status") == 200, history_days=0, coverage=None,
        trade_history_rows=th.get("rows"), trade_history_span_ms=th.get("span_ms"), has_side=th.get("has_side"),
        sampling_frequency="tick", pagination="REST returns only the most recent trades", rate_limit=KU_RATE,
        point_in_time=True, timestamp_semantics="per-trade ts (exchange time)", lookahead_risk="PASS",
        lookahead_evidence="tick timestamps", parity="PARITY_POSSIBLE_WITH_IMPLEMENTATION",
        parity_note="kline volume has no taker split; history requires prospective WebSocket collection",
        prospective_collection_required=True)
    d20 = live.get("depth20", {})
    add(id="KUCOIN_ORDER_BOOK", family="ORDER_BOOK_MICROSTRUCTURE", provider="KuCoin", venue="KUCOIN_FUTURES",
        metric="L2 depth (spread, imbalance, microprice, slope)",
        availability="VERIFIED_LIVE_ONLY" if d20.get("status") == 200 else "UNREACHABLE", auth="NONE",
        cost_class="FREE_PUBLIC", historical_endpoint=None,
        runtime_endpoint="REST /api/v1/level2/depth20 ; WebSocket /contractMarket/level2Depth50:{symbol}",
        live_endpoint_verified=d20.get("status") == 200, history_days=0, coverage=None,
        sequence_updates_per_sec=d20.get("sequence_updates_per_sec"), snapshot_bytes=d20.get("bytes"),
        sampling_frequency="sub-second", pagination="n/a", rate_limit=KU_RATE, point_in_time=True,
        timestamp_semantics="snapshot ts + sequence", lookahead_risk="PASS", lookahead_evidence="live only",
        parity="PARITY_POSSIBLE_WITH_IMPLEMENTATION", parity_note="no public book history; prospective only",
        prospective_collection_required=True)
    liq_ok = cand.get("liquidation_orders", {}).get("status") == 200 and cand["liquidation_orders"].get("error") is None
    add(id="KUCOIN_LIQUIDATIONS", family="LIQUIDATIONS", provider="KuCoin", venue="KUCOIN_FUTURES",
        metric="liquidation orders", availability="VERIFIED_LIVE_ONLY" if liq_ok else "UNAVAILABLE", auth="NONE",
        cost_class="FREE_PUBLIC" if liq_ok else "UNAVAILABLE", historical_endpoint=None, runtime_endpoint=None,
        live_endpoint_verified=liq_ok, history_days=0, coverage=None, probe=cand.get("liquidation_orders"),
        sampling_frequency="n/a", pagination="n/a", rate_limit=KU_RATE, point_in_time=None,
        timestamp_semantics="no verified public liquidation feed", lookahead_risk="FAIL" if not liq_ok else "PASS",
        lookahead_evidence="source not available", parity="REJECTED" if not liq_ok else "PARITY_POSSIBLE_WITH_IMPLEMENTATION",
        parity_note="no verified KuCoin public liquidation history or stream")
    ls_ok = cand.get("long_short_ratio", {}).get("status") == 200 and cand["long_short_ratio"].get("error") is None
    add(id="KUCOIN_POSITIONING", family="DERIVATIVES_POSITIONING", provider="KuCoin", venue="KUCOIN_FUTURES",
        metric="long/short ratio", availability="VERIFIED" if ls_ok else "UNAVAILABLE", auth="NONE",
        cost_class="FREE_PUBLIC" if ls_ok else "UNAVAILABLE", historical_endpoint=None, runtime_endpoint=None,
        history_days=0, coverage=None, probe=cand.get("long_short_ratio"), sampling_frequency="n/a", pagination="n/a",
        rate_limit=KU_RATE, point_in_time=None, timestamp_semantics="no verified public endpoint",
        lookahead_risk="FAIL", lookahead_evidence="source not available", parity="REJECTED",
        parity_note="no verified KuCoin public positioning data")
    # Binance vision archives (CROSS_SOURCE_PROXY)
    for sid, kind, fam, metric, sem, lk in (
            ("BINANCE_OPEN_INTEREST", "metrics", "OPEN_INTEREST", "sum_open_interest (5m)",
             "create_time = 5m snapshot instant; period convention (start/end) not documented unambiguously -> "
             "observed_ts = create_time + 15 min conservative lag", "PASS_CONSERVATIVE_LAG"),
            ("BINANCE_POSITIONING", "metrics", "DERIVATIVES_POSITIONING",
             "top-trader / global long-short ratios, taker long/short vol ratio (5m)",
             "as open interest (same file); conservative lag", "PASS_CONSERVATIVE_LAG"),
            ("BINANCE_TAKER_FLOW", "klines_1h", "AGGRESSOR_TAKER_FLOW", "taker_buy_volume (1h klines)",
             "kline open_time; taker buy volume known at bar close", "PASS"),
            ("BINANCE_FUNDING", "funding", "FUNDING", "funding rate (settled)",
             "calc_time = settlement instant", "PASS"),
            ("BINANCE_PREMIUM_INDEX", "premium_1h", "MARK_INDEX_BASIS", "premium index 1h klines",
             "kline open_time; value known at bar close", "PASS")):
        qq = q.get(f"binance_vision_{kind}", {})
        b0, b1 = _span(qq)
        files = [v.get("files", {}) for v in qq.values()]
        okf = sum(f.get("files_ok", 0) for f in files)
        add(id=sid, family=fam, provider="Binance (data.binance.vision archive)", venue="BINANCE_USDM", metric=metric,
            availability="VERIFIED" if okf else "UNREACHABLE", auth="NONE", cost_class="FREE_PUBLIC",
            historical_endpoint=f"https://data.binance.vision/data/futures/um/.../{kind} (zip + .CHECKSUM)",
            runtime_endpoint="Binance USD-M public REST (not the execution venue; reachability from runtime unverified)",
            first_ts=b0, last_ts=b1, history_days=_days(max(b0 or 0, aa.WINDOW_START_MS), b1) if b0 else 0,
            symbols_available=sum(1 for v in qq.values() if v.get("rows_in_window")),
            coverage=_min_cov(qq) if kind not in ("funding",) else (1.0 if okf and b0 else None),
            gaps=sum(v.get("gap_count", 0) or 0 for v in qq.values()),
            checksums={"ok": sum(f.get("checksum_ok", 0) for f in files), "bad": sum(f.get("checksum_bad", 0) for f in files),
                       "files_ok": okf, "files": sum(f.get("files", 0) for f in files)},
            sampling_frequency={"metrics": "5m", "klines_1h": "1h", "funding": "8h settlement",
                                "premium_1h": "1h"}[kind],
            pagination="one file per day/month", rate_limit=BN_RATE, point_in_time=True, timestamp_semantics=sem,
            lookahead_risk=lk if okf else "FAIL", lookahead_evidence="archive timestamps + conservative lag where ambiguous",
            parity="CROSS_SOURCE_PROXY",
            parity_note="history from Binance; runtime venue is KuCoin -> not promotable without a venue-equivalence "
                        "study and a runtime adapter for the same Binance source",
            revision_behavior="archive files are checksummed; single retrieval (no revision comparison)")
    # third-party REST probes
    for sid, fam, key in (("BINANCE_REST_OI_HIST", "OPEN_INTEREST", "binance_oi_hist_548d"),
                          ("BINANCE_REST_FORCE_ORDERS", "LIQUIDATIONS", "binance_force_orders"),
                          ("BINANCE_VISION_LIQUIDATIONS", "LIQUIDATIONS", "binance_vision_liquidation_snapshot_60d_ago"),
                          ("BYBIT_OPEN_INTEREST", "OPEN_INTEREST", "bybit_open_interest_548d"),
                          ("BYBIT_FUNDING", "FUNDING", "bybit_funding_548d"),
                          ("BYBIT_ACCOUNT_RATIO", "DERIVATIVES_POSITIONING", "bybit_account_ratio"),
                          ("OKX_FUNDING_HISTORY", "FUNDING", "okx_funding_history"),
                          ("OKX_OI_HISTORY", "OPEN_INTEREST", "okx_oi_history")):
        p = tp.get(key, {})
        reach = p.get("status") == 200 and (p.get("rows") or 0) > 0
        add(id=sid, family=fam, provider=sid.split("_")[0].title(), venue=sid.split("_")[0] + "_PERP", metric=key,
            availability="VERIFIED_PROBE" if reach else "UNREACHABLE", auth="NONE", cost_class="FREE_PUBLIC",
            historical_endpoint=key, runtime_endpoint="provider public REST", probe=p,
            history_days=548 if (reach and "548d" in key) else 0, coverage=None, sampling_frequency="see probe",
            pagination="documented per provider", rate_limit="DOCUMENTED per provider", point_in_time=None,
            live_endpoint_verified=reach, timestamp_semantics="not audited beyond reachability (probe)",
            lookahead_risk="FAIL", lookahead_evidence="semantics not verified in this audit (unresolved => FAIL)",
            parity="CROSS_SOURCE_PROXY" if reach else "REJECTED",
            parity_note="probe only; HTTP status recorded from the CI runner region")
    # cross-sectional (derived, same source as Phase 8F)
    xs = r.get("xsec") or {}
    par = (xs.get("parity") or {}).get("parity")
    for sid, fam in (("XSEC_CROSS_SECTIONAL", "CROSS_SECTIONAL_MARKET_STATE"), ("XSEC_MARKET_REGIME", "MARKET_WIDE_REGIME"),
                     ("XSEC_VOLUME_VOLATILITY", "VOLUME_VOLATILITY_INFORMATION")):
        add(id=sid, family=fam, provider="KuCoin (derived from the Phase 8F exact 1h dataset)", venue="KUCOIN_FUTURES",
            metric="CROSS_SECTIONAL_FEATURES_V1 subset", availability="VERIFIED" if xs else "UNAVAILABLE", auth="NONE",
            cost_class="FREE_PUBLIC", historical_endpoint="GET /api/v1/kline/query (1h), 12 symbols",
            runtime_endpoint="same kline endpoint (closed bars only)", first_ts=aa.WINDOW_START_MS,
            last_ts=aa.WINDOW_END_MS if xs else None, history_days=548 if xs else 0, symbols_available=12 if xs else 0,
            coverage=1.0 if xs and r.get("price_dataset_sha256") else None, gaps=0, sampling_frequency="1h",
            pagination="<=150 bars/request", rate_limit=KU_RATE, point_in_time=True,
            timestamp_semantics="value at T uses only bars with close <= T; symbol present only with the bar closing at T",
            lookahead_risk="PASS" if par else "FAIL",
            lookahead_evidence="research/runtime parity + future-mutation leakage tests",
            parity="PARITY_READY" if par else "REJECTED", parity_note="two independent implementations, parity checked",
            derived_from_price=True)
    return src


def storage_estimates(r: dict) -> dict:
    out = {"basis": "canonical bytes measured on downloaded data; prospective sources estimated from live probes",
           "downloaded": {}}
    for m in r["manifests"]:
        days = max(1.0, (m["last_ts"] - m["first_ts"]) / aa.DAY) if m.get("first_ts") else None
        if days:
            per_day = m["bytes_canonical"] / days
            out["downloaded"][m["dataset"]] = {"rows_per_day_12_symbols": round(m["rows"] / days, 1),
                                               "canonical_mb_per_day": round(per_day / 1e6, 4),
                                               "canonical_gb_per_year": round(per_day * 365 / 1e9, 4)}
    lv = r["kucoin_live"]
    trades = []
    for s, v in lv.items():
        th = v.get("trade_history", {})
        if th.get("rows") and th.get("span_ms"):
            trades.append(th["rows"] / max(1, th["span_ms"]) * 1000 * 86400)
    tpd = float(sum(trades) / len(trades)) if trades else None
    upd = [v.get("depth20", {}).get("sequence_updates_per_sec") for v in lv.values()]
    upd = [u for u in upd if u is not None]
    ups = float(sum(upd) / len(upd)) if upd else None
    snap = [v.get("depth20", {}).get("bytes") for v in lv.values() if v.get("depth20", {}).get("bytes")]
    sb = float(sum(snap) / len(snap)) if snap else None
    out["prospective"] = {
        "taker_trades": {"rows_per_day_12_symbols": round(tpd * 12) if tpd else None,
                         "mb_per_day_raw_at_120B": round(tpd * 12 * 120 / 1e6, 1) if tpd else None,
                         "gb_per_year_raw": round(tpd * 12 * 120 * 365 / 1e9, 1) if tpd else None,
                         "canonical_1m_bars_mb_per_day": round(12 * 1440 * 64 / 1e6, 2),
                         "basis": "trades/day from the span of the latest REST trade page (BTC and DOGE)"},
        "order_book": {"l2_updates_per_sec_per_symbol": ups,
                       "raw_incremental_gb_per_year_at_80B": round(ups * 86400 * 12 * 80 * 365 / 1e9, 1) if ups else None,
                       "depth20_snapshot_1s_gb_per_year": round(sb * 86400 * 12 * 365 / 1e9, 1) if sb else None,
                       "canonical_1m_features_mb_per_day": round(12 * 1440 * 8 * 16 / 1e6, 2)},
        "open_interest_1m_snapshots": {"rows_per_day_12_symbols": 12 * 1440, "mb_per_day": round(12 * 1440 * 96 / 1e6, 2),
                                       "gb_per_year": round(12 * 1440 * 96 * 365 / 1e9, 3)},
        "mark_index_predicted_funding_1m": {"rows_per_day_12_symbols": 12 * 1440 * 3,
                                            "gb_per_year": round(12 * 1440 * 3 * 96 * 365 / 1e9, 3)}}
    return out


COLLECTOR = {
    "status": "DESIGN ONLY - NOT DEPLOYED",
    "principles": ["public endpoints only, no credentials", "append-only raw + canonical ALPHA_DATA_CONTRACT_V1 records",
                   "event_ts from the exchange, observed_ts = local receipt (NTP-disciplined UTC)",
                   "dedup key (source, venue, symbol, metric, event_ts[, sequence])",
                   "gap detection from exchange sequence numbers / expected cadence; gaps stay missing",
                   "daily partition files with sha256 manifests; restart resumes from the last committed partition",
                   "never runs inside the production trading service"],
    "streams": {
        "open_interest": {"endpoint": "REST GET /api/v1/contracts/{symbol} (openInterest)", "cadence": "60 s poll",
                          "event_ts": "request time (no exchange timestamp on the field)", "retention": "indefinite canonical"},
        "mark_index_predicted_funding": {"endpoint": "REST /api/v1/mark-price/{symbol}/current + /api/v1/funding-rate/{symbol}/current "
                                                     "(or WebSocket /contract/instrument:{symbol}, DOCUMENTED)",
                                         "cadence": "60 s", "event_ts": "timePoint field", "retention": "indefinite"},
        "taker_trades": {"endpoint": "WebSocket /contractMarket/execution:{symbol} (DOCUMENTED)", "cadence": "tick",
                         "event_ts": "trade ts", "dedup": "tradeId", "canonical": "1m signed volume bars",
                         "retention": "raw 30 d, canonical indefinite"},
        "order_book": {"endpoint": "WebSocket /contractMarket/level2Depth50:{symbol} (DOCUMENTED) + periodic REST depth20 resync",
                       "cadence": "snapshot every 1 s -> 1m features", "event_ts": "exchange ts",
                       "gap_detection": "sequence continuity", "retention": "raw 7 d, canonical 1m indefinite"},
        "liquidations": {"status": "no verified KuCoin public source - not collectable"}},
    "persistence": "object storage (daily gz JSONL per stream/symbol) + manifest table; separate from evidence Postgres",
    "restart_recovery": "resume from last manifest; REST backfill only where the endpoint serves history; otherwise mark gap",
    "integrity": "per-partition sha256 over canonical records; manifest chain hash",
    "minimum_before_8G_B": "12 months of continuous collection (>= 365 d) before a prospective source is eligible"}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Phase 8G-A data feasibility audit (read-only)")
    ap.add_argument("--price-dataset", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--code-sha", required=True)
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    r = asyncio.run(aa.audit(a.price_dataset, a.out))

    def dump(name, obj):
        (out / name).write_text(json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n")
    sources = build_sources(r)
    classes = {c: [s["id"] for s in sources if s["phase_8g_b_class"] == c]
               for c in ("READY_FOR_8G_B", "NEEDS_ENGINEERING", "PROSPECTIVE_ONLY", "REJECTED")}
    fields = ("id", "family", "provider", "metric", "venue", "history_start", "history_end", "history_days",
              "symbols_available", "sampling_frequency", "coverage", "gaps", "pagination", "rate_limit", "auth",
              "cost_class", "point_in_time", "lookahead_risk", "runtime_endpoint", "historical_endpoint", "parity",
              "phase_8g_b_class", "availability")
    dump("alpha_source_inventory.json", [{k: s.get(k) for k in fields} for s in sources])
    dump("source_provenance.json", {"retrieved_at": r["retrieved_at"], "http": r["http_log_summary"],
                                    "kucoin_contracts": r["kucoin_contracts"], "kucoin_live": r["kucoin_live"],
                                    "kucoin_candidate_paths": r["kucoin_candidate_paths"],
                                    "third_party_rest": r["third_party_rest"],
                                    "evidence_levels": {"VERIFIED": "measured in this run",
                                                        "DOCUMENTED": "provider documentation, not verified here"}})
    dump("historical_depth_report.json", {"kucoin_funding_depth_probe": r["kucoin_funding_depth"],
                                          "kucoin_index_query_probe": r["kucoin_index_query_probe"],
                                          "kucoin_premium_query_probe": r["kucoin_premium_query_probe"],
                                          "per_source": {s["id"]: {k: s.get(k) for k in ("history_start", "history_end",
                                                                                         "history_days", "symbols_available")}
                                                         for s in sources},
                                          "min_history_days": aa.MIN_HISTORY_DAYS, "ideal_history_days": aa.IDEAL_HISTORY_DAYS})
    dump("coverage_report.json", {"per_dataset_per_symbol": r["quality"],
                                  "summary": {k: aa.summarize_quality(v, "coverage_vs_8h_grid" if k == "kucoin_funding" else "coverage")
                                              for k, v in r["quality"].items() if k != "kucoin_basis_from_index_kline"}})
    dump("timestamp_semantics.json", {s["id"]: {"timestamp_semantics": s.get("timestamp_semantics"),
                                                "sampling_frequency": s.get("sampling_frequency"),
                                                "point_in_time": s.get("point_in_time"),
                                                "revision_behavior": s.get("revision_behavior")} for s in sources})
    dump("lookahead_risk_report.json", {s["id"]: {"lookahead_risk": s["lookahead_risk"],
                                                  "evidence": s.get("lookahead_evidence")} for s in sources})
    dump("runtime_parity_matrix.json", {s["id"]: {"parity": s["parity"], "note": s.get("parity_note"),
                                                  "runtime_endpoint": s.get("runtime_endpoint")} for s in sources})
    dump("cross_source_consistency.json", {"role": "PROVENANCE_INTEGRITY_ONLY_NOT_ALPHA", **r["cross_source"],
                                           "kucoin_basis_from_index_kline": r["quality"].get("kucoin_basis_from_index_kline")})
    from bot.ai import xsec_features as xf
    dump("cross_sectional_feature_contract.json", {"schema": xf.SCHEMA, "schema_sha256": xf.SCHEMA_SHA256,
                                                   "audit": r.get("xsec"), "price_dataset_sha256": r.get("price_dataset_sha256")})
    dump("storage_estimates.json", storage_estimates(r))
    dump("prospective_collector_design.json", COLLECTOR)
    dump("alpha_data_contract.json", {"contract": aa.ALPHA_DATA_CONTRACT, "sha256": aa.ALPHA_DATA_CONTRACT_SHA256})
    dump("dataset_manifests.json", r["manifests"])
    exogenous = [s for s in classes["READY_FOR_8G_B"] if not next(x for x in sources if x["id"] == s).get("derived_from_price")]
    derived = [s for s in classes["READY_FOR_8G_B"] if next(x for x in sources if x["id"] == s).get("derived_from_price")]
    if exogenous or derived:
        result = "ALPHA_DATA_READY"
    elif classes["PROSPECTIVE_ONLY"] or classes["NEEDS_ENGINEERING"]:
        result = "ONLY_PROSPECTIVE_DATA_AVAILABLE"
    else:
        result = "NO_NEW_RELIABLE_ALPHA_DATA"

    def fam_status(fam):
        xs = [s for s in sources if s["family"] == fam]
        return {s["id"]: s["phase_8g_b_class"] for s in xs}
    summary = {"version": aa.VERSION, "phase_8g_a_result": result, "code_sha": a.code_sha,
               "alpha_data_contract_sha256": aa.ALPHA_DATA_CONTRACT_SHA256, "sources_audited": len(sources),
               **{k.lower(): v for k, v in classes.items()},
               "ready_exogenous_non_price": exogenous, "ready_derived_from_price": derived,
               "cross_sectional_price_features_ready": bool(derived) and all(
                   next(x for x in sources if x["id"] == s)["parity"] == "PARITY_READY" for s in derived),
               "family_status": {f: fam_status(f) for f in sorted({s["family"] for s in sources})},
               "prospective_collection_required": [s["id"] for s in sources if s.get("prospective_collection_required")],
               "price_dataset_sha256": r.get("price_dataset_sha256"), "window": r["window"],
               "outcomes_used": False, "models_trained": False,
               "classification_rule": {"ALPHA_DATA_READY": ">= 1 source READY_FOR_8G_B (exogenous and price-derived "
                                                           "reported separately)",
                                       "ONLY_PROSPECTIVE_DATA_AVAILABLE": "no READY source; >= 1 prospective or engineering source",
                                       "NO_NEW_RELIABLE_ALPHA_DATA": "otherwise"}}
    dump("phase8g_a_summary.json", summary)
    print(json.dumps(summary, indent=1, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
