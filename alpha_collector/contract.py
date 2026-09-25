"""PROSPECTIVE_ALPHA_DATA_CONTRACT_V1 and every Phase 8H policy, as hashed data.

Operationally separate from the trading bot: this package imports nothing from
``bot`` and has no order, account or private-key capability.
"""
from __future__ import annotations

import hashlib
import json

COLLECTOR_VERSION = "ALPHA_PROSPECTIVE_COLLECTOR_V1"
CONTRACT_VERSION = "PROSPECTIVE_ALPHA_DATA_CONTRACT_V1"
EPOCH_ID = "PHASE8H_EPOCH_V1"
PREFLIGHT_EPOCH_ID = "PREFLIGHT_ONLY"
DB_AUTHORITY = "BGX_RESEARCH_PROSPECTIVE_ALPHA_V1"
DB_SCHEMA = "alpha_prospective"
SCHEMA_VERSION = 1
SOURCE = "KUCOIN_FUTURES_PUBLIC"
VENUE = "KUCOIN_FUTURES"
CLOCK_TOLERANCE_MS = 2_000
SKEW_ALARM_MS = 2_000


def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


SYMBOL_MAPPING = {"BTCUSDT": "XBTUSDTM", "ETHUSDT": "ETHUSDTM", "SOLUSDT": "SOLUSDTM", "XRPUSDT": "XRPUSDTM",
                  "ADAUSDT": "ADAUSDTM", "DOGEUSDT": "DOGEUSDTM", "LINKUSDT": "LINKUSDTM", "AVAXUSDT": "AVAXUSDTM",
                  "DOTUSDT": "DOTUSDTM", "LTCUSDT": "LTCUSDTM", "NEARUSDT": "NEARUSDTM", "ATOMUSDT": "ATOMUSDTM"}
SYMBOLS = tuple(SYMBOL_MAPPING)

SOURCE_ENDPOINTS = {
    "rest_base": "https://api-futures.kucoin.com",
    "server_time": "GET /api/v1/timestamp",
    "contracts": "GET /api/v1/contracts/active (mapping + multiplier validation)",
    "open_interest": "GET /api/v1/contracts/{venue_symbol} field openInterest (lots, no venue timestamp)",
    "funding_live": "GET /api/v1/funding-rate/{venue_symbol}/current fields value, predictedValue, timePoint, granularity",
    "mark_index": "GET /api/v1/mark-price/{venue_symbol}/current fields value (mark), indexPrice, timePoint",
    "book_snapshot": "GET /api/v1/level2/snapshot?symbol={venue_symbol} fields sequence, bids, asks, ts",
    "ws_token": "POST /api/v1/bullet-public (public token; never persisted or logged)",
    "ws_trades": "/contractMarket/execution:{venue_symbols} subject match: tradeId, sequence, side, size, price, ts(ns)",
    "ws_book": "/contractMarket/level2:{venue_symbols} subject level2: sequence, change 'price,side,size', timestamp",
    "semantics": {
        "trade_side": "DOCUMENTED: execution `side` is the taker (aggressor) side; recorded as field `taker_side_venue` "
                      "and never re-derived",
        "level2_change": "DOCUMENTED: 'price,side,size' where size is the NEW absolute size at that price (0 = remove)",
        "open_interest": "DOCUMENTED: openInterest in lots (contracts); base quantity = lots * multiplier",
        "predicted_funding": "DOCUMENTED: predictedValue = estimate for the NEXT settlement at time of request; "
                             "value = current period rate; timePoint = rate timestamp",
    },
    "auth": "none (public endpoints only; no API key, no HMAC, no passphrase)"}

SAMPLING_POLICY = {"open_interest_poll_s": 60, "funding_mark_poll_s": 60, "server_time_poll_s": 60,
                   "book_feature_snapshot_s": 5, "book_depth_levels": [5, 10], "trade_flow_bucket_s": 60,
                   "heartbeat_s": 30, "health_flush_s": 60,
                   "rationale": "book updates are ~100-250/s/symbol (8G-A probe); persisting every delta costs TB/year, "
                                "so the local book is maintained in memory and deterministic features are persisted every 5s"}

AGGREGATION = {
    "trade_flow_1m": {"bucket": "floor(event_ts_ms / 60000) * 60000 (venue event time)",
                      "fields": {"buy_notional": "sum(price*size*multiplier) where taker_side_venue == buy",
                                 "sell_notional": "same for sell", "total_notional": "buy + sell",
                                 "buy_count": "count buy", "sell_count": "count sell", "total_count": "count",
                                 "imbalance": "(buy_notional - sell_notional) / total_notional (null if 0)",
                                 "vwap": "sum(price*size) / sum(size)", "high": "max price", "low": "min price"},
                      "arithmetic": "Decimal on the venue strings; reproducible exactly from trade_events",
                      "completeness": "a bucket is final once the stream passed bucket end + 5 s with no gap "
                                      "flagged; otherwise flagged INCOMPLETE"},
    "book_features": {"bid1/ask1/bid1_size/ask1_size": "best levels", "spread": "ask1 - bid1",
                      "mid": "(bid1 + ask1)/2", "microprice": "(bid1*ask1_size + ask1*bid1_size)/(bid1_size+ask1_size)",
                      "depth_bid_N/depth_ask_N": "sum of sizes (lots) of the best N levels, N in {5,10}",
                      "imbalance_N": "(depth_bid_N - depth_ask_N)/(depth_bid_N + depth_ask_N)",
                      "book_slope": "NOT IMPLEMENTED in V1 (no unambiguous definition predeclared)",
                      "validity": "only emitted when book_state == VALID; otherwise a gap is recorded"}}

CANONICAL_FIELDS = ("contract_version", "collector_version", "source", "venue", "symbol", "metric", "event_ts",
                    "observed_ts", "ingested_ts", "sequence", "value", "units", "quality_flags", "source_channel",
                    "raw_payload_sha256", "collector_instance_id", "collection_epoch_id")
METRICS = {"open_interest_lots": "lots", "open_interest_base": "base_asset",
           "open_interest_notional_quote": "quote_usdt (lots*multiplier*mark from the same poll; DERIVED)",
           "trade": "lots (value = size; price in payload)", "funding_rate_current": "rate_per_interval",
           "funding_rate_predicted": "rate_per_interval", "mark_price": "quote_usdt", "index_price": "quote_usdt",
           "book_features": "mixed (see AGGREGATION.book_features)", "trade_flow_1m": "quote_usdt / counts"}
QUALITY_FLAGS = ("MISSING", "EVENT_TS_FROM_OBSERVATION", "CLOCK_ORDER_VIOLATION", "FUTURE_EVENT_TS", "DUPLICATE",
                 "OUT_OF_ORDER", "BOOK_INVALID", "INCOMPLETE_BUCKET", "DERIVED", "PREFLIGHT_ONLY", "STALE")
FORBIDDEN_COLUMNS = ("pnl", "profit", "return", "label", "target", "prediction", "outcome", "r_multiple",
                     "future_", "signal", "position")

CANONICAL_SCHEMA = {"fields": list(CANONICAL_FIELDS), "metrics": METRICS, "quality_flags": list(QUALITY_FLAGS),
                    "timestamps": {"event_ts": "venue event time (ms); when the venue gives none, event_ts = "
                                               "observed_ts and EVENT_TS_FROM_OBSERVATION is set",
                                   "observed_ts": "local receipt time (ms, NTP-disciplined host clock)",
                                   "ingested_ts": "database commit time (ms)",
                                   "rule": f"event_ts <= observed_ts + {CLOCK_TOLERANCE_MS} and observed_ts <= "
                                           f"ingested_ts + {CLOCK_TOLERANCE_MS}; violations flagged, never repaired"},
                    "missing": "value null + MISSING flag; never 0", "no_outcomes": list(FORBIDDEN_COLUMNS)}

RETENTION_POLICY = {"oi_snapshots": "permanent", "funding_live_snapshots": "permanent", "mark_index_snapshots": "permanent",
                    "trade_flow_1m": "permanent", "book_feature_snapshots": "permanent",
                    "trade_events": "raw trades permanent in V1 (estimated tens of GB/year; revisit only via EPOCH_V2)",
                    "raw_book_deltas": "NOT persisted: the in-memory book is sequence-checked and 5 s feature snapshots "
                                       "are persisted; information between snapshots is lost (documented)",
                    "gaps/heartbeats/health/manifests": "permanent", "deletion": "none; no silent deletion"}

INTEGRITY_POLICY = {"daily_manifest": "per UTC day, per table: row counts, first/last event_ts, gap count, duplicate "
                                      "rejections, quality-flag counts, sha256 over the canonical rows ordered by key",
                    "seal": "a day is SEALED after day end + 1 h grace if its manifest is written; DB triggers reject "
                            "INSERT/UPDATE/DELETE of rows in sealed (table, day); corrections are appended as new "
                            "correction records in data_corrections", "digest": "sha256 of newline-joined per-row "
                                                                              "sha256 in key order (Merkle-like list)"}

GAP_POLICY = {"open_interest": "no successful poll within 2 x poll interval -> gap row",
              "funding_mark": "same rule", "trades": "websocket disconnect / no message for 60 s on a subscribed "
                                                     "topic -> gap row (start, end)",
              "book": "sequence break, resync, disconnect -> BOOK_INVALID gap row until a successful resync",
              "fill": "gaps are data; nothing is back-filled with zeros or interpolation"}

BOOK_POLICY = {"init": "subscribe first, buffer deltas, then REST snapshot; apply buffered deltas with sequence > "
                       "snapshot.sequence; the first applied delta must be snapshot.sequence + 1",
               "apply": "sequence == last + 1 -> apply; sequence <= last -> duplicate/out-of-order, ignored + counted; "
                        "sequence > last + 1 -> gap -> BOOK_INVALID and forced REST resync",
               "valid_features_only": True, "resync_backoff_s": [1, 2, 4, 8, 16, 30]}

HEALTH_CONTRACT = {"fields": ["source", "channel", "symbol", "window_start", "window_end", "requests", "successes",
                              "rate_limited", "retries", "parse_failures", "ws_reconnects", "sequence_gaps",
                              "resyncs", "last_event_ts", "last_write_ts", "max_lag_ms", "clock_skew_ms"],
                   "alarms": {"clock_skew_ms_gt": SKEW_ALARM_MS, "no_write_s_gt": 180, "reconnects_per_hour_gt": 12}}

RETRY_POLICY = {"base_s": 1.0, "factor": 2.0, "max_s": 60.0, "jitter": "full jitter U(0, delay)",
                "rate_limit": "HTTP 429 -> honour Retry-After / gw-ratelimit-reset else backoff; never tighter loop"}

EPOCH_SCHEMA = {"fields": ["epoch_id", "code_sha", "contract_sha256", "canonical_schema_sha256", "db_schema_sha256",
                           "symbol_mapping", "contract_multipliers", "source_endpoints_sha256", "sampling_policy_sha256",
                           "aggregation_sha256", "t0_ms", "created_at_ms"],
                "rules": ["immutable once written", "t0 never moves backward", "material change => new epoch id"]}

CONTRACT = {"version": CONTRACT_VERSION, "collector_version": COLLECTOR_VERSION, "source": SOURCE, "venue": VENUE,
            "symbols": SYMBOL_MAPPING, "canonical_schema": CANONICAL_SCHEMA, "source_endpoints": SOURCE_ENDPOINTS,
            "sampling": SAMPLING_POLICY, "aggregation": AGGREGATION, "retention": RETENTION_POLICY,
            "integrity": INTEGRITY_POLICY, "gaps": GAP_POLICY, "book": BOOK_POLICY, "health": HEALTH_CONTRACT,
            "retry": RETRY_POLICY, "epoch": EPOCH_SCHEMA, "db": {"authority": DB_AUTHORITY, "schema": DB_SCHEMA,
                                                                 "schema_version": SCHEMA_VERSION},
            "no_outcomes": "market-data evidence store only: no returns, PnL, labels, targets or predictions",
            "no_execution": "no order methods, no private keys, no execution lease, no account mutation"}

SHAS = {"PROSPECTIVE_ALPHA_DATA_CONTRACT_SHA256": sha(CONTRACT), "CANONICAL_SCHEMA_SHA256": sha(CANONICAL_SCHEMA),
        "COLLECTION_EPOCH_SCHEMA_SHA256": sha(EPOCH_SCHEMA), "SYMBOL_MAPPING_SHA256": sha(SYMBOL_MAPPING),
        "SOURCE_ENDPOINT_MANIFEST_SHA256": sha(SOURCE_ENDPOINTS), "SAMPLING_POLICY_SHA256": sha(SAMPLING_POLICY),
        "RETENTION_POLICY_SHA256": sha(RETENTION_POLICY), "INTEGRITY_POLICY_SHA256": sha(INTEGRITY_POLICY)}
CONTRACT_SHA256 = SHAS["PROSPECTIVE_ALPHA_DATA_CONTRACT_SHA256"]


def epoch_manifest(*, code_sha: str, db_schema_sha256: str, multipliers: dict, t0_ms: int, created_at_ms: int,
                   epoch_id: str = EPOCH_ID) -> dict:
    m = {"epoch_id": epoch_id, "code_sha": code_sha, "contract_sha256": CONTRACT_SHA256,
         "canonical_schema_sha256": SHAS["CANONICAL_SCHEMA_SHA256"], "db_schema_sha256": db_schema_sha256,
         "symbol_mapping": SYMBOL_MAPPING, "contract_multipliers": {k: str(v) for k, v in sorted(multipliers.items())},
         "source_endpoints_sha256": SHAS["SOURCE_ENDPOINT_MANIFEST_SHA256"],
         "sampling_policy_sha256": SHAS["SAMPLING_POLICY_SHA256"], "aggregation_sha256": sha(AGGREGATION),
         "t0_ms": int(t0_ms), "created_at_ms": int(created_at_ms)}
    m["manifest_sha256"] = sha(m)
    return m


def raw_sha(payload) -> str:
    if isinstance(payload, (bytes, bytearray)):
        return hashlib.sha256(payload).hexdigest()
    return sha(payload)


def make_record(*, symbol, metric, event_ts, observed_ts, value, units, source_channel, raw, instance_id, epoch_id,
                sequence=None, flags=(), payload=None) -> dict:
    """Canonical record. Missing stays missing; timestamp order violations are flagged, never repaired."""
    fl = set(flags)
    v = None
    if value is not None:
        try:
            v = str(value) if not isinstance(value, str) else value
            float(v)
        except (TypeError, ValueError):
            v = None
    if v is None:
        fl.add("MISSING")
    if event_ts is None:
        event_ts = observed_ts
        fl.add("EVENT_TS_FROM_OBSERVATION")
    if int(event_ts) > int(observed_ts) + CLOCK_TOLERANCE_MS:
        fl.add("CLOCK_ORDER_VIOLATION")
        fl.add("FUTURE_EVENT_TS")
    if epoch_id == PREFLIGHT_EPOCH_ID:
        fl.add("PREFLIGHT_ONLY")
    return {"contract_version": CONTRACT_VERSION, "collector_version": COLLECTOR_VERSION, "source": SOURCE,
            "venue": VENUE, "symbol": symbol, "metric": metric, "event_ts": int(event_ts),
            "observed_ts": int(observed_ts), "ingested_ts": None, "sequence": None if sequence is None else int(sequence),
            "value": v, "units": units, "quality_flags": sorted(fl), "source_channel": source_channel,
            "raw_payload_sha256": raw_sha(raw), "collector_instance_id": instance_id, "collection_epoch_id": epoch_id,
            "payload": payload or {}}


def check_ingest(rec: dict, ingested_ts: int) -> dict:
    rec = dict(rec, ingested_ts=int(ingested_ts))
    if rec["observed_ts"] > int(ingested_ts) + CLOCK_TOLERANCE_MS:
        rec["quality_flags"] = sorted(set(rec["quality_flags"]) | {"CLOCK_ORDER_VIOLATION"})
    return rec
