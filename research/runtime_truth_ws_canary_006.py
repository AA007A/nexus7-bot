"""BGX-RUNTIME-TRUTH-006 isolated public-market WebSocket canary.

Runs only in BGX-RESEARCH. No private exchange credentials, DB, orders, risk,
or execution path. Exercises the exact KuCoinClient public _ws_loop with all
12 production symbols x 3 strategy timeframes plus ticker while runtime-truth
transport capture and exporter are enabled.
"""
from __future__ import annotations

import asyncio
import json
import os

# Fail closed against accidental use as a trading workload.
os.environ.setdefault("PAPER_TRADE", "true")

from bot import runtime_truth as truth
from bot import runtime_truth_exporter
from bot import runtime_truth_hooks as hooks
from bot.kucoin import KuCoinClient

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT",
    "LINKUSDT", "AVAXUSDT", "DOTUSDT", "LTCUSDT", "NEARUSDT", "ATOMUSDT",
]
INTERVALS = ["15", "60", "240"]


async def main() -> None:
    if not truth.enabled():
        raise SystemExit("BGX_RUNTIME_TRUTH_ENABLED must be true for canary")
    if os.environ.get("PAPER_TRADE", "").strip().lower() != "true":
        raise SystemExit("canary requires PAPER_TRADE=true")

    hooks.install_transport_and_cache(KuCoinClient)
    client = KuCoinClient()
    runtime_truth_exporter.start()
    task = None
    try:
        instruments = await client.load_instruments()
        missing = [s for s in SYMBOLS if s not in instruments]
        if missing:
            raise RuntimeError(f"missing_public_instruments={missing}")

        task = asyncio.create_task(client._ws_loop(SYMBOLS, INTERVALS))
        await asyncio.sleep(float(os.environ.get("BGX_CANARY_SECONDS", "55")))

        cache = getattr(client, "_kline_cache", {}) or {}
        cache_rows = sum(len(v) for v in cache.values())
        report = {
            "BGX_RUNTIME_TRUTH_006_CANARY": "OBSERVATION_COMPLETE",
            "runtime_instance_id": truth.runtime_instance_id(),
            "deployment_sha": truth.deployment_sha(),
            "paper_trade": True,
            "symbols": len(SYMBOLS),
            "intervals": len(INTERVALS),
            "expected_subscriptions": len(SYMBOLS) * len(INTERVALS) + 1,
            "ws_acks": int(getattr(client, "_ws_acks", 0)),
            "ws_errors": int(getattr(client, "_ws_errors", 0)),
            "cache_series": len(cache),
            "cache_rows": cache_rows,
            "last_ws_update": float(getattr(client, "_last_ws_update", 0.0)),
            "telemetry_dropped_total": truth.RECORDER.telemetry_dropped_total,
            "export_dropped_total": truth.RECORDER.export_dropped_total,
            "oversized_total": truth.RECORDER.oversized_total,
            "sequence_watermark": truth.RECORDER.current_sequence,
            "exchange_action": "NONE",
            "private_credentials_used": False,
        }
        print("BGX_CANARY_REPORT=" + json.dumps(report, sort_keys=True), flush=True)
        if report["ws_acks"] != report["expected_subscriptions"]:
            raise RuntimeError(f"WS_ACK_PARITY_FAIL={report['ws_acks']}/{report['expected_subscriptions']}")
        if report["ws_errors"] != 0:
            raise RuntimeError(f"WS_SUBSCRIPTION_ERRORS={report['ws_errors']}")
        if report["last_ws_update"] <= 0:
            raise RuntimeError("NO_LIVE_WS_MARKET_UPDATE_OBSERVED")
        if any(report[k] != 0 for k in ("telemetry_dropped_total", "export_dropped_total", "oversized_total")):
            raise RuntimeError("TRUTH_DROP_COUNTER_NONZERO")
        print("BGX_RUNTIME_TRUTH_006_CANARY=PASS", flush=True)
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                print(f"CANARY_WS_TASK_END={type(exc).__name__}:{exc}", flush=True)
        await runtime_truth_exporter.stop()
        try:
            await client.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
