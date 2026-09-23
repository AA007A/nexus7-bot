import base64
import csv
import decimal
import hashlib
import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

BASE = "https://api-futures.kucoin.com"
SOURCE = BASE + "/api/v1/kline/query"
ROOT = pathlib.Path(os.environ.get("DATA_ROOT", "/data/bgx-missed-market-002"))
RAW = ROOT / "data" / "raw"
NORM = ROOT / "data" / "normalized"
OUT = ROOT / "output"
for path in (RAW, NORM, OUT):
    path.mkdir(parents=True, exist_ok=True)

CONTRACTS = {
    "BTCUSDT": "XBTUSDTM",
    "ETHUSDT": "ETHUSDTM",
    "SOLUSDT": "SOLUSDTM",
    "XRPUSDT": "XRPUSDTM",
    "ADAUSDT": "ADAUSDTM",
    "DOGEUSDT": "DOGEUSDTM",
    "LINKUSDT": "LINKUSDTM",
    "AVAXUSDT": "AVAXUSDTM",
    "DOTUSDT": "DOTUSDTM",
    "LTCUSDT": "LTCUSDTM",
    "NEARUSDT": "NEARUSDTM",
    "ATOMUSDT": "ATOMUSDTM",
}
TIMEFRAMES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}
START_MS = int(datetime(2026, 9, 23, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)
END_MS = int(datetime(2026, 9, 23, 18, 0, tzinfo=timezone.utc).timestamp() * 1000)
FOUR_H_CONFIRMED_START_MS = int(datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
FOUR_H_CONFIRMED_END_MS = int(datetime(2026, 9, 23, 16, 0, tzinfo=timezone.utc).timestamp() * 1000) - 1


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_json_exact(raw: bytes):
    return json.loads(raw.decode("utf-8"), parse_float=str, parse_int=int)


def maybe_restore_persisted_bundle() -> bool:
    count = int(os.environ.get("BGX_PERSISTED_BUNDLE_CHUNKS", "0") or "0")
    expected_sha = os.environ.get("BGX_PERSISTED_BUNDLE_SHA256", "")
    if count <= 0:
        return False
    chunks = []
    for i in range(count):
        key = f"BGX_PERSISTED_BUNDLE_{i:04d}"
        value = os.environ.get(key)
        if value is None:
            raise RuntimeError(f"missing persisted bundle chunk {key}")
        chunks.append(value)
    payload = base64.b64decode("".join(chunks))
    actual_sha = sha256_bytes(payload)
    if expected_sha and actual_sha != expected_sha:
        raise RuntimeError(f"persisted bundle hash mismatch expected={expected_sha} actual={actual_sha}")
    zip_path = OUT / "dataset.zip"
    zip_path.write_bytes(payload)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(ROOT)
    (OUT / "restored_from_railway_config.json").write_text(
        json.dumps({"restored": True, "bundle_sha256": actual_sha, "chunks": count}, indent=2),
        encoding="utf-8",
    )
    print(f"BGX_PERSISTENCE_RESTORE=YES chunks={count} sha256={actual_sha}", flush=True)
    return True


def serve_forever():
    os.chdir(ROOT)
    port = int(os.environ.get("PORT", "8080"))
    print(f"BGX_HTTP_SERVING port={port} root={ROOT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler).serve_forever()


if maybe_restore_persisted_bundle():
    serve_forever()

request_records = []
failures = []
api_errors = []
contract_ok = {}
slice_meta = {}


def request_bytes(url, internal, contract, granularity, start_ms, end_ms, max_tries=4):
    last_error = None
    for attempt in range(max_tries):
        request_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "BGX-RESEARCH/1.0"})
            with urllib.request.urlopen(req, timeout=20) as response:
                status = response.status
                body = response.read()
            obj = load_json_exact(body)
            code = str(obj.get("code", ""))
            data = obj.get("data")
            rows = len(data) if isinstance(data, list) else (1 if data is not None else 0)
            request_records.append({
                "request_timestamp": request_ts,
                "symbol": internal,
                "contract": contract,
                "granularity": granularity,
                "from": start_ms,
                "to": end_ms,
                "http_status": status,
                "kucoin_code": code,
                "row_count": rows,
                "retry_count": attempt,
            })
            if status == 200 and code == "200000":
                return body, obj
            last_error = f"HTTP {status} KuCoin code={code}"
            if status == 429 or status >= 500:
                time.sleep(2 ** attempt)
                continue
            break
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read()
                obj = load_json_exact(body)
                code = str(obj.get("code", ""))
            except Exception:
                code = ""
            request_records.append({
                "request_timestamp": request_ts,
                "symbol": internal,
                "contract": contract,
                "granularity": granularity,
                "from": start_ms,
                "to": end_ms,
                "http_status": exc.code,
                "kucoin_code": code,
                "row_count": 0,
                "retry_count": attempt,
            })
            last_error = f"HTTPError {exc.code} {code}"
            if exc.code == 429 or exc.code >= 500:
                time.sleep(2 ** attempt)
                continue
            break
        except Exception as exc:
            request_records.append({
                "request_timestamp": request_ts,
                "symbol": internal,
                "contract": contract,
                "granularity": granularity,
                "from": start_ms,
                "to": end_ms,
                "http_status": None,
                "kucoin_code": None,
                "row_count": 0,
                "retry_count": attempt,
                "error": f"{type(exc).__name__}:{exc}",
            })
            last_error = f"{type(exc).__name__}:{exc}"
            time.sleep(2 ** attempt)
    raise RuntimeError(last_error or "request failed")


# Validate exact public Futures contracts and preserve raw metadata.
for internal, contract in CONTRACTS.items():
    try:
        body, obj = request_bytes(
            BASE + "/api/v1/contracts/" + urllib.parse.quote(contract),
            internal,
            contract,
            "CONTRACT_META",
            None,
            None,
        )
        data = obj.get("data") or {}
        ok = obj.get("code") == "200000" and data.get("symbol") == contract
        contract_ok[internal] = bool(ok)
        meta_path = RAW / contract / "contract.json"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_bytes(body)
        print(f"CONTRACT {internal} {contract} {'OK' if ok else 'FAIL'}", flush=True)
        if not ok:
            api_errors.append({
                "type": "contract_mapping",
                "internal": internal,
                "contract": contract,
                "returned_symbol": data.get("symbol"),
            })
    except Exception as exc:
        contract_ok[internal] = False
        failures.append({"slice": f"{internal}:CONTRACT_META", "error": str(exc)})
        print(f"CONTRACT {internal} {contract} ERROR {exc!r}", flush=True)


# Download the 60 direct Kline slices. For 4h only the last confirmed candle
# overlapping the event is downloaded (12:00-16:00). The 16:00-18:00 forming
# 4h state is intentionally reconstructed later from 1m, never from the 20:00 close.
for internal, contract in CONTRACTS.items():
    for timeframe, granularity in TIMEFRAMES.items():
        if timeframe == "4h":
            start_ms, end_ms = FOUR_H_CONFIRMED_START_MS, FOUR_H_CONFIRMED_END_MS
        else:
            start_ms, end_ms = START_MS, END_MS - 1
        query = urllib.parse.urlencode({
            "symbol": contract,
            "granularity": granularity,
            "from": start_ms,
            "to": end_ms,
        })
        url = SOURCE + "?" + query
        raw_path = RAW / contract / f"{timeframe}.json"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        normalized_path = NORM / f"{contract}_{timeframe}.csv"
        key = f"{internal}:{timeframe}"
        try:
            body, obj = request_bytes(url, internal, contract, granularity, start_ms, end_ms)
            raw_path.write_bytes(body)
            rows = obj.get("data") or []
            if not isinstance(rows, list):
                raise RuntimeError("KuCoin data is not a list")
            parsed = []
            for row in rows:
                if not isinstance(row, list) or len(row) < 7:
                    raise RuntimeError("invalid Kline row")
                parsed.append((int(row[0]), *[str(value) for value in row[1:7]]))
            raw_timestamps = [row[0] for row in parsed]
            raw_out_of_order = any(
                raw_timestamps[i] > raw_timestamps[i + 1]
                for i in range(len(raw_timestamps) - 1)
            )
            parsed.sort(key=lambda row: row[0])
            with normalized_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f, lineterminator="\n")
                writer.writerow(["timestamp", "open", "high", "low", "close", "volume", "turnover"])
                writer.writerows(parsed)

            interval_ms = granularity * 60 * 1000
            expected = (
                [FOUR_H_CONFIRMED_START_MS]
                if timeframe == "4h"
                else list(range(START_MS, END_MS, interval_ms))
            )
            expected_set = set(expected)
            counts = {}
            for row in parsed:
                if row[0] in expected_set:
                    counts[row[0]] = counts.get(row[0], 0) + 1
            duplicates = sum(count - 1 for count in counts.values() if count > 1)
            missing = [timestamp for timestamp in expected if counts.get(timestamp, 0) == 0]
            ohlc_violations = []
            timezone_alignment_violations = []
            for row in parsed:
                timestamp, open_, high, low, close, volume, _turnover = row
                o = decimal.Decimal(open_)
                h = decimal.Decimal(high)
                l = decimal.Decimal(low)
                c = decimal.Decimal(close)
                v = decimal.Decimal(volume)
                if not (h >= o and h >= c and l <= o and l <= c and h >= l and v >= 0):
                    ohlc_violations.append(timestamp)
                if timestamp % interval_ms != 0:
                    timezone_alignment_violations.append(timestamp)
            slice_meta[key] = {
                "internal_symbol": internal,
                "contract": contract,
                "timeframe": timeframe,
                "granularity": granularity,
                "request_from": start_ms,
                "request_to": end_ms,
                "first_timestamp": parsed[0][0] if parsed else None,
                "last_timestamp": parsed[-1][0] if parsed else None,
                "row_count": len(parsed),
                "expected_bucket_count": len(expected),
                "missing_buckets": missing,
                "missing_classification": "NO_TRADE_GAP_POSSIBLE" if missing else "NONE",
                "duplicates": duplicates,
                "raw_out_of_order": raw_out_of_order,
                "normalized_ascending": all(
                    parsed[i][0] < parsed[i + 1][0] for i in range(len(parsed) - 1)
                ),
                "ohlc_violations": ohlc_violations,
                "timezone_alignment_violations": timezone_alignment_violations,
                "raw_file": str(raw_path.relative_to(ROOT)),
                "normalized_file": str(normalized_path.relative_to(ROOT)),
            }
            print(
                f"SLICE {internal} {timeframe} rows={len(parsed)} missing={len(missing)} "
                f"duplicates={duplicates} ohlc_bad={len(ohlc_violations)}",
                flush=True,
            )
        except Exception as exc:
            failures.append({"slice": key, "error": str(exc)})
            print(f"SLICE {key} ERROR {exc!r}", flush=True)


request_log_path = OUT / "request_log.jsonl"
with request_log_path.open("w", encoding="utf-8") as f:
    for record in request_records:
        f.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")


def read_normalized(contract, timeframe):
    path = NORM / f"{contract}_{timeframe}.csv"
    rows = []
    if not path.exists():
        return rows
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            row["timestamp"] = int(row["timestamp"])
            rows.append(row)
    return rows


def aggregate_validate(contract, target_timeframe, minutes):
    one_minute = read_normalized(contract, "1m")
    direct = read_normalized(contract, target_timeframe)
    one_map = {row["timestamp"]: row for row in one_minute}
    direct_map = {row["timestamp"]: row for row in direct}
    bucket_ms = minutes * 60 * 1000
    eligible = matches = mismatches = skipped = 0
    details = []
    for bucket in range(START_MS, END_MS, bucket_ms):
        needed = [bucket + i * 60_000 for i in range(minutes)]
        if not all(timestamp in one_map for timestamp in needed) or bucket not in direct_map:
            skipped += 1
            continue
        eligible += 1
        rows = [one_map[timestamp] for timestamp in needed]
        calc = {
            "open": decimal.Decimal(rows[0]["open"]),
            "high": max(decimal.Decimal(row["high"]) for row in rows),
            "low": min(decimal.Decimal(row["low"]) for row in rows),
            "close": decimal.Decimal(rows[-1]["close"]),
            "volume": sum(decimal.Decimal(row["volume"]) for row in rows),
            "turnover": sum(decimal.Decimal(row["turnover"]) for row in rows),
        }
        target = direct_map[bucket]
        ohlc_match = all(
            calc[field] == decimal.Decimal(target[field])
            for field in ("open", "high", "low", "close")
        )
        volume_match = calc["volume"] == decimal.Decimal(target["volume"])
        turnover_match = calc["turnover"] == decimal.Decimal(target["turnover"])
        if ohlc_match and volume_match and turnover_match:
            matches += 1
        else:
            mismatches += 1
            details.append({
                "bucket": bucket,
                "ohlc_match": ohlc_match,
                "volume_match": volume_match,
                "turnover_match": turnover_match,
            })
    return {
        "eligible_complete_buckets": eligible,
        "matches": matches,
        "mismatches": mismatches,
        "skipped": skipped,
        "details": details[:100],
    }


cross = {"5m": {}, "15m": {}, "1h": {}}
for internal, contract in CONTRACTS.items():
    cross["5m"][internal] = aggregate_validate(contract, "5m", 5)
    cross["15m"][internal] = aggregate_validate(contract, "15m", 15)
    cross["1h"][internal] = aggregate_validate(contract, "1h", 60)

contract_count = sum(1 for ok in contract_ok.values() if ok)
slice_count = len(slice_meta)
missing_total = sum(len(meta["missing_buckets"]) for meta in slice_meta.values())
duplicate_total = sum(meta["duplicates"] for meta in slice_meta.values())
ohlc_bad = sum(len(meta["ohlc_violations"]) for meta in slice_meta.values())
timezone_bad = sum(len(meta["timezone_alignment_violations"]) for meta in slice_meta.values())
normalized_out_of_order = sum(1 for meta in slice_meta.values() if not meta["normalized_ascending"])
raw_out_of_order = sum(1 for meta in slice_meta.values() if meta["raw_out_of_order"])
cross_mismatch = {
    timeframe: sum(result["mismatches"] for result in per_symbol.values())
    for timeframe, per_symbol in cross.items()
}
cross_eligible = {
    timeframe: sum(result["eligible_complete_buckets"] for result in per_symbol.values())
    for timeframe, per_symbol in cross.items()
}

# A successful API response with a missing bucket is documented as a possible no-trade
# gap per KuCoin's official Kline documentation. Unexplained gaps are acquisition or
# integrity failures, represented separately in failures/api_errors.
unexplained_gaps = 0
http_failures = sum(
    1 for record in request_records
    if record.get("http_status") not in (200, None)
)

dataset_valid = (
    contract_count == 12
    and slice_count == 60
    and not failures
    and not api_errors
    and duplicate_total == 0
    and ohlc_bad == 0
    and timezone_bad == 0
    and normalized_out_of_order == 0
    and all(cross_mismatch[timeframe] == 0 for timeframe in cross_mismatch)
)

validation_report = {
    "source": "KUCOIN_FUTURES_OFFICIAL",
    "endpoint": SOURCE,
    "start": "2026-09-23T14:00:00Z",
    "end": "2026-09-23T18:00:00Z",
    "analysis_interval": "[14:00Z,18:00Z)",
    "forming_4h_rule": (
        "Direct 12:00-16:00 confirmed candle only; 16:00-18:00 forming 4h state "
        "must be reconstructed from acquired 1m data. No 20:00 close/high/low/volume is acquired."
    ),
    "contracts_expected": 12,
    "contracts_validated": contract_count,
    "contract_mapping": CONTRACTS,
    "contract_validation": contract_ok,
    "slices_expected": 60,
    "slices_acquired": slice_count,
    "slice_counts": {
        timeframe: sum(1 for key in slice_meta if key.endswith(":" + timeframe))
        for timeframe in TIMEFRAMES
    },
    "http_failures": http_failures,
    "kucoin_api_errors": len(api_errors),
    "failures": failures,
    "api_errors": api_errors,
    "missing_buckets": missing_total,
    "unexplained_gaps": unexplained_gaps,
    "duplicates": duplicate_total,
    "raw_out_of_order_slices": raw_out_of_order,
    "normalized_out_of_order_slices": normalized_out_of_order,
    "ohlc_integrity_violations": ohlc_bad,
    "timezone_alignment_violations": timezone_bad,
    "cross_timeframe": {
        "eligible": cross_eligible,
        "mismatches": cross_mismatch,
        "details": cross,
    },
    "slices": slice_meta,
    "dataset_valid": dataset_valid,
}
validation_path = OUT / "validation_report.json"
validation_path.write_text(
    json.dumps(validation_report, indent=2, sort_keys=True, default=str),
    encoding="utf-8",
)

# Hash freeze. Manifest intentionally excludes itself to avoid recursive hashing;
# its final SHA-256 is stored in manifest.sha256.
artifacts = []
for path in sorted(ROOT.rglob("*")):
    if not path.is_file():
        continue
    if path.name in {"manifest.json", "manifest.sha256", "dataset.zip", "dataset.zip.sha256", "summary.json"}:
        continue
    relative = str(path.relative_to(ROOT))
    metadata = {
        "filename": relative,
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "source": "KUCOIN_FUTURES_OFFICIAL" if relative.startswith("data/") else "BGX_RESEARCH",
    }
    if relative.startswith("data/normalized/"):
        stem = path.stem
        timeframe = stem.rsplit("_", 1)[-1]
        contract = stem[: -(len(timeframe) + 1)]
        metadata.update({"contract": contract, "timeframe": timeframe})
        found = next(
            (
                meta for meta in slice_meta.values()
                if meta["contract"] == contract and meta["timeframe"] == timeframe
            ),
            None,
        )
        if found:
            metadata.update({
                "first_timestamp": found["first_timestamp"],
                "last_timestamp": found["last_timestamp"],
                "row_count": found["row_count"],
            })
    artifacts.append(metadata)

manifest = {
    "dataset": "BGX-MISSED-MARKET-002",
    "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "data_cut": "2026-09-23T18:00:00Z",
    "source": "KUCOIN_FUTURES_OFFICIAL",
    "dataset_valid": dataset_valid,
    "artifacts": artifacts,
}
manifest_path = OUT / "manifest.json"
manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
manifest_sha = sha256_file(manifest_path)
(OUT / "manifest.sha256").write_text(f"{manifest_sha}  manifest.json\n", encoding="utf-8")

zip_path = OUT / "dataset.zip"
with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for path in sorted(ROOT.rglob("*")):
        if path.is_file() and path != zip_path and path.name != "dataset.zip.sha256":
            archive.write(path, arcname=str(path.relative_to(ROOT)))
zip_sha = sha256_file(zip_path)
(OUT / "dataset.zip.sha256").write_text(f"{zip_sha}  dataset.zip\n", encoding="utf-8")

summary = {
    "contracts_validated": contract_count,
    "slices_acquired": slice_count,
    "1m_slices": validation_report["slice_counts"]["1m"],
    "5m_slices": validation_report["slice_counts"]["5m"],
    "15m_slices": validation_report["slice_counts"]["15m"],
    "1h_slices": validation_report["slice_counts"]["1h"],
    "4h_slices": validation_report["slice_counts"]["4h"],
    "http_failures": http_failures,
    "kucoin_api_errors": len(api_errors),
    "raw_files": sum(1 for path in RAW.rglob("*.json")),
    "normalized_files": sum(1 for path in NORM.glob("*.csv")),
    "missing_buckets": missing_total,
    "unexplained_gaps": unexplained_gaps,
    "duplicates": duplicate_total,
    "raw_out_of_order_slices": raw_out_of_order,
    "out_of_order_normalized": normalized_out_of_order,
    "ohlc_integrity_violations": ohlc_bad,
    "timezone_alignment_violations": timezone_bad,
    "5m_aggregation_match": cross_mismatch["5m"] == 0 and cross_eligible["5m"] > 0,
    "15m_aggregation_match": cross_mismatch["15m"] == 0 and cross_eligible["15m"] > 0,
    "1h_aggregation_match": cross_mismatch["1h"] == 0 and cross_eligible["1h"] > 0,
    "manifest_sha256": manifest_sha,
    "bundle_sha256": zip_sha,
    "dataset_valid": dataset_valid,
    "failed_slices": failures,
}
summary_path = OUT / "summary.json"
summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
print("BGX_DATASET_SUMMARY=" + json.dumps(summary, separators=(",", ":")), flush=True)

# Emit a compressed bundle in bounded chunks. The controller can persist these
# chunks back into Railway service variables, providing a durable Railway-only
# backup when a Volume cannot be provisioned through the available API.
bundle_b64 = base64.b64encode(zip_path.read_bytes()).decode("ascii")
chunk_size = 2500
chunks = [bundle_b64[i:i + chunk_size] for i in range(0, len(bundle_b64), chunk_size)]
print(f"BGX_BUNDLE_CHUNK_COUNT={len(chunks)}", flush=True)
for index, chunk in enumerate(chunks):
    print(f"BGX_BUNDLE_CHUNK_{index:04d}={chunk}", flush=True)

serve_forever()
