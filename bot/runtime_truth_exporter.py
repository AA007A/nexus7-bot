"""Async fail-open exporter for BGX runtime truth telemetry.

No trading/exchange modules are imported here. Remote I/O occurs only in this
background task. Retry exhaustion drops telemetry and increments counters; it
never blocks trading.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone

import aiohttp

from bot import runtime_truth

_BATCH_MAX_EVENTS = int(os.environ.get("BGX_TRUTH_BATCH_MAX_EVENTS", "256"))
_BATCH_MAX_BYTES = int(os.environ.get("BGX_TRUTH_BATCH_MAX_BYTES", "524288"))
_FLUSH_SECONDS = float(os.environ.get("BGX_TRUTH_BATCH_FLUSH_SECONDS", "0.5"))
_MAX_RETRIES = int(os.environ.get("BGX_TRUTH_EXPORT_RETRIES", "3"))
_TIMEOUT_SECONDS = float(os.environ.get("BGX_TRUTH_EXPORT_TIMEOUT_SECONDS", "2.0"))
_CHECKPOINT_SECONDS = float(os.environ.get("BGX_TRUTH_CHECKPOINT_SECONDS", "900"))


def _utc_day(event: dict) -> str:
    return str(event.get("timestamp_utc", ""))[:10]


def _split_by_utc_day(events: list[dict]) -> list[list[dict]]:
    """Keep sink batches within one UTC day without reordering events."""
    groups: list[list[dict]] = []
    for event in events:
        if not groups or _utc_day(groups[-1][-1]) != _utc_day(event):
            groups.append([event])
        else:
            groups[-1].append(event)
    return groups


class TruthExporter:
    def __init__(self):
        self.enabled = runtime_truth.enabled()
        self.base_url = os.environ.get("BGX_TRUTH_SINK_URL", "").rstrip("/")
        self.token = os.environ.get("BGX_TRUTH_INGEST_TOKEN", "")
        self._task = None
        self._stop = asyncio.Event()
        self._last_checkpoint = time.monotonic()
        self.export_latency_ms: list[float] = []
        self.export_failures = 0
        self.export_retries = 0
        self.exported_events = 0
        self.exported_bytes = 0

    def configured(self) -> bool:
        return bool(self.enabled and self.base_url and self.token)

    def start(self):
        if not self.configured() or self._task is not None:
            return self._task
        self._task = asyncio.create_task(self._run(), name="bgx-runtime-truth-exporter")
        return self._task

    async def stop(self):
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=max(1.0, _TIMEOUT_SECONDS * 2))
        except Exception:
            self._task.cancel()
        self._task = None

    async def _post_sink(self, session: aiohttp.ClientSession, path: str, body: dict) -> bool:
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        for attempt in range(max(1, _MAX_RETRIES)):
            t0 = time.perf_counter()
            try:
                async with session.post(self.base_url + path, data=raw, headers=headers) as response:
                    await response.read()
                    latency = (time.perf_counter() - t0) * 1000.0
                    self.export_latency_ms.append(latency)
                    self.export_latency_ms = self.export_latency_ms[-1000:]
                    if 200 <= response.status < 300:
                        self.exported_bytes += len(raw)
                        return True
            except Exception:
                pass
            self.export_failures += 1
            if attempt + 1 < max(1, _MAX_RETRIES):
                self.export_retries += 1
                await asyncio.sleep(min(0.25 * (2 ** attempt), 1.0))
        return False

    async def _export_events(self, session: aiohttp.ClientSession, events: list[dict]) -> None:
        for batch in _split_by_utc_day(events):
            body = {
                "events": batch,
                "reported_dropped_event_count": runtime_truth.RECORDER.reported_dropped_total(),
            }
            if await self._post_sink(session, "/v1/events/batch", body):
                self.exported_events += len(batch)
            else:
                runtime_truth.RECORDER.mark_export_drop(len(batch))

    def _checkpoint(self, reason: str) -> dict | None:
        provider = runtime_truth.RECORDER.checkpoint_provider()
        if provider is None:
            return None
        try:
            snapshot = provider()
            if not isinstance(snapshot, dict):
                return None
            return {
                "schema_version": "bgx-runtime-truth-checkpoint-v1",
                "runtime_instance_id": runtime_truth.runtime_instance_id(),
                "deployment_sha": runtime_truth.deployment_sha(),
                "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                "sequence_watermark": runtime_truth.RECORDER.current_sequence,
                "cache_state": snapshot.get("cache_state", {}),
                "provenance": snapshot.get("provenance", {}),
                "cache_data_hashes": snapshot.get("cache_data_hashes", {}),
                "provenance_hashes": snapshot.get("provenance_hashes", {}),
                "reason": reason,
                "reported_dropped_event_count": runtime_truth.RECORDER.reported_dropped_total(),
                "stream_sealed": bool(snapshot.get("stream_sealed", False) or reason == "SHUTDOWN_BEST_EFFORT"),
            }
        except Exception:
            return None

    async def _run(self):
        timeout = aiohttp.ClientTimeout(total=max(0.2, _TIMEOUT_SECONDS))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while not self._stop.is_set():
                batch = runtime_truth.RECORDER.drain(_BATCH_MAX_EVENTS, _BATCH_MAX_BYTES)
                if batch:
                    await self._export_events(session, batch)
                reason = runtime_truth.RECORDER.pop_checkpoint_request()
                now = time.monotonic()
                if reason is None and _CHECKPOINT_SECONDS > 0 and now - self._last_checkpoint >= _CHECKPOINT_SECONDS:
                    reason = "PERIODIC"
                if reason is not None:
                    checkpoint = self._checkpoint(reason)
                    if checkpoint is not None:
                        await self._post_sink(session, "/v1/checkpoints", checkpoint)
                    self._last_checkpoint = now
                if not batch and reason is None:
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=max(0.05, _FLUSH_SECONDS))
                    except asyncio.TimeoutError:
                        pass
            runtime_truth.emit("MARKET_STREAM_SEAL", payload={"reason": "SHUTDOWN_BEST_EFFORT"})
            batch = runtime_truth.RECORDER.drain(_BATCH_MAX_EVENTS, _BATCH_MAX_BYTES)
            if batch:
                await self._export_events(session, batch)
            checkpoint = self._checkpoint("SHUTDOWN_BEST_EFFORT")
            if checkpoint is not None:
                await self._post_sink(session, "/v1/checkpoints", checkpoint)

    def metrics(self) -> dict:
        return {
            "export_latency_ms": list(self.export_latency_ms),
            "export_failures": self.export_failures,
            "export_retries": self.export_retries,
            "exported_events": self.exported_events,
            "exported_bytes": self.exported_bytes,
        }


EXPORTER = TruthExporter()

def start():
    return EXPORTER.start()

async def stop():
    await EXPORTER.stop()

def metrics() -> dict:
    return EXPORTER.metrics()
