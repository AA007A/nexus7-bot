import asyncio
import aiohttp
import json
import os
import time
import uuid

import websockets

from bot import runtime_truth as rt
from bot import runtime_truth_exporter as ex
from bot import runtime_truth_rest as rr

BASE = "https://api-futures.kucoin.com"
SYMBOL_KC = "LTCUSDTM"
SYMBOL = "LTCUSDT"
CACHE = {}


def normalize_rows(document):
    rows = []
    for row in document.get("data") or []:
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            continue
        ts = int(float(row[0]))
        if ts < 100_000_000_000:
            ts *= 1000
        rows.append({
            "ts": ts,
            "o": float(row[1]),
            "h": float(row[2]),
            "l": float(row[3]),
            "c": float(row[4]),
            "v": float(row[6]),
        })
    rows.sort(key=lambda x: x["ts"])
    return rows[-120:]


async def fetch_tf(session, granularity):
    now_ms = int(time.time() * 1000)
    width = {"15": 900_000, "60": 3_600_000, "240": 14_400_000}[granularity]
    params = {
        "symbol": SYMBOL_KC,
        "granularity": granularity,
        "from": str(now_ms - width * 120),
        "to": str(now_ms),
    }
    async with session.get(BASE + "/api/v1/kline/query", params=params, timeout=20) as response:
        raw = await response.read()
        document = json.loads(raw.decode("utf-8"))
        rr.remember_raw_kline_fields(raw)
        raw_event_id = rt.capture_rest_response(
            "/api/v1/kline/query", dict(response.url.query), response.status, raw
        )
        rows = normalize_rows(document)
        if not rows:
            raise RuntimeError(f"no rows for timeframe={granularity}: {document}")
        CACHE[granularity] = rows
        raw_fields = rr.current_raw_kline_fields()
        rt.capture_cache_mutation(
            symbol=SYMBOL,
            timeframe=granularity,
            before_rows=[],
            after_rows=rows,
            mutation_action="CACHE_EXTEND",
            source="CANARY_PUBLIC_REST",
            raw_event_id=raw_event_id,
            normalized_before=None,
            normalized_after=rows[-1],
            raw_volume_fields=raw_fields.get(rows[-1]["ts"], {}),
        )
        return rows


async def capture_ws_sample(session):
    async with session.post(BASE + "/api/v1/bullet-public", timeout=20) as response:
        bullet = await response.json()
    data = bullet.get("data") or {}
    token = data.get("token")
    servers = data.get("instanceServers") or []
    if not token or not servers:
        raise RuntimeError(f"invalid bullet-public response: {bullet}")

    endpoint = servers[0]["endpoint"]
    connection_id = "bgx-canary-" + uuid.uuid4().hex
    uri = endpoint + "?token=" + token + "&connectId=" + connection_id
    topic = "/contractMarket/limitCandle:LTCUSDTM_1min"
    before = []
    count = 0
    deadline = time.monotonic() + 35

    async with websockets.connect(uri, ping_interval=None, close_timeout=5) as ws:
        subscribe = {
            "id": str(int(time.time() * 1000)),
            "type": "subscribe",
            "topic": topic,
            "privateChannel": False,
            "response": True,
        }
        await ws.send(json.dumps(subscribe, separators=(",", ":")))
        while time.monotonic() < deadline and count < 8:
            try:
                payload = await asyncio.wait_for(ws.recv(), timeout=8)
            except asyncio.TimeoutError:
                continue
            text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
            try:
                message = json.loads(text)
            except Exception:
                continue
            if message.get("type") != "message" or message.get("topic") != topic:
                continue
            candles = (message.get("data") or {}).get("candles") or []
            if len(candles) < 7:
                continue

            raw_event_id = rt.capture_ws_application_payload(text, connection_id)
            ts = int(float(candles[0]))
            if ts < 100_000_000_000:
                ts *= 1000
            row = {
                "ts": ts,
                "o": float(candles[1]),
                "h": float(candles[3]),
                "l": float(candles[4]),
                "c": float(candles[2]),
                "v": float(candles[6]),
            }
            after = [dict(x) for x in before]
            index = next((i for i, item in enumerate(after) if int(item["ts"]) == ts), None)
            if index is None:
                after.append(row)
                after.sort(key=lambda x: x["ts"])
                action = "APPEND" if after[-1]["ts"] == ts else "OUT_OF_ORDER_INSERT"
                previous = None
            else:
                previous = dict(before[index])
                after[index] = row
                action = "REPLACE_EXISTING"

            rt.capture_cache_mutation(
                symbol=SYMBOL,
                timeframe="1",
                before_rows=before,
                after_rows=after,
                mutation_action=action,
                source="WS_UPDATE",
                raw_event_id=raw_event_id,
                normalized_before=previous,
                normalized_after=row,
                raw_volume_fields={
                    "ws_index_5_volume": candles[5],
                    "ws_index_6_amount": candles[6],
                },
            )
            before = after
            count += 1

    CACHE["1"] = before
    return count


def checkpoint_snapshot():
    cache_state = {}
    provenance = {}
    cache_hashes = {}
    provenance_hashes = {}
    for timeframe, rows in CACHE.items():
        key = f"{SYMBOL}|{timeframe}"
        cache_state[key] = [dict(x) for x in rows]
        prov = rt.RECORDER.provenance_for(SYMBOL, timeframe, rows)
        provenance[key] = prov
        cache_hashes[key] = rt.cache_data_hash(rows)
        provenance_hashes[key] = rt.provenance_hash(prov)
    return {
        "cache_state": cache_state,
        "provenance": provenance,
        "cache_data_hashes": cache_hashes,
        "provenance_hashes": provenance_hashes,
        "stream_sealed": True,
    }


async def cohort_status(session):
    token = os.environ["BGX_TRUTH_INGEST_TOKEN"]
    base = os.environ["BGX_TRUTH_SINK_URL"].rstrip("/")
    url = base + "/v1/cohorts/" + rt.runtime_instance_id() + "/status"
    headers = {"Authorization": "Bearer " + token}
    async with session.get(url, headers=headers, timeout=20) as response:
        body = await response.json()
        return response.status, body


async def main():
    if not rt.enabled():
        raise RuntimeError("BGX_RUNTIME_TRUTH_ENABLED must be true")

    ex.start()
    async with aiohttp.ClientSession() as session:
        for timeframe in ("15", "60", "240"):
            await fetch_tf(session, timeframe)

        analysis_inputs = {tf: CACHE[tf] for tf in ("15", "60", "240")}
        rt.capture_analysis_input(
            SYMBOL,
            analysis_inputs,
            analysis_inputs,
            int(time.time() * 1000),
        )

        ws_messages = await capture_ws_sample(session)

        rt.register_checkpoint_provider(checkpoint_snapshot)
        rt.request_checkpoint("CANARY_SEAL")
        await asyncio.sleep(3)
        await ex.stop()
        await asyncio.sleep(1)
        status_http, status = await cohort_status(session)

    producer_metrics = rt.metrics()
    exporter_metrics = ex.metrics()
    result = {
        "runtime_instance_id": rt.runtime_instance_id(),
        "deployment_sha": rt.deployment_sha(),
        "ws_messages": ws_messages,
        "telemetry_dropped_total": producer_metrics.get("telemetry_dropped_total"),
        "export_dropped_total": producer_metrics.get("export_dropped_total"),
        "oversized_total": producer_metrics.get("oversized_total"),
        "queue_depth": producer_metrics.get("queue_depth"),
        "queue_bytes": producer_metrics.get("queue_bytes"),
        "export_failures": exporter_metrics.get("export_failures"),
        "export_retries": exporter_metrics.get("export_retries"),
        "exported_events": exporter_metrics.get("exported_events"),
        "exported_bytes": exporter_metrics.get("exported_bytes"),
        "status_http": status_http,
        "event_stream_complete": status.get("EVENT_STREAM_COMPLETE"),
        "missing_sequences": status.get("missing_sequences"),
        "reported_dropped_event_count": status.get("reported_dropped_event_count"),
        "hashes_verified": status.get("hashes_verified"),
        "sealed": status.get("sealed"),
        "persisted_event_count": status.get("persisted_event_count"),
        "first_sequence": status.get("first_sequence"),
        "last_sequence": status.get("last_sequence"),
    }
    print("BGX_RT004_CANARY_RESULT=" + json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
