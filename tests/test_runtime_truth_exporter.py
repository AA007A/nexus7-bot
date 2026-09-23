import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from bot import runtime_truth as truth
from bot import runtime_truth_exporter as exporter_module
from bot.runtime_truth_exporter import TruthExporter, _split_by_utc_day


class _FailSession:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0
    def post(self, *args, **kwargs):
        self.calls += 1
        raise self.exc


class RuntimeTruthExporterTests(unittest.TestCase):
    def test_batches_split_at_utc_day_without_reordering(self):
        events = [
            {"timestamp_utc":"2026-09-23T23:59:59.900000Z","monotonic_sequence":1},
            {"timestamp_utc":"2026-09-23T23:59:59.999999Z","monotonic_sequence":2},
            {"timestamp_utc":"2026-09-24T00:00:00.000001Z","monotonic_sequence":3},
            {"timestamp_utc":"2026-09-24T00:00:01Z","monotonic_sequence":4},
        ]
        groups = _split_by_utc_day(events)
        self.assertEqual([[e["monotonic_sequence"] for e in g] for g in groups], [[1,2],[3,4]])

    def test_sink_unavailable_retries_are_bounded(self):
        exporter = TruthExporter()
        exporter.base_url = "https://sink.invalid"
        exporter.token = "telemetry-only"
        session = _FailSession(ConnectionError("sink unavailable"))
        with patch.object(exporter_module, "_MAX_RETRIES", 3), patch("asyncio.sleep", new=AsyncMock()):
            ok = asyncio.run(exporter._post_sink(session, "/v1/events/batch", {"events": []}))
        self.assertFalse(ok)
        self.assertEqual(session.calls, 3)
        self.assertEqual(exporter.export_retries, 2)
        self.assertEqual(exporter.export_failures, 3)

    def test_sink_timeout_retries_are_bounded(self):
        exporter = TruthExporter()
        exporter.base_url = "https://sink.invalid"
        exporter.token = "telemetry-only"
        session = _FailSession(asyncio.TimeoutError())
        with patch.object(exporter_module, "_MAX_RETRIES", 2), patch("asyncio.sleep", new=AsyncMock()):
            ok = asyncio.run(exporter._post_sink(session, "/v1/checkpoints", {"x": 1}))
        self.assertFalse(ok)
        self.assertEqual(session.calls, 2)
        self.assertEqual(exporter.export_retries, 1)

    def test_export_failure_drops_telemetry_not_trading_state(self):
        old_enabled, old_recorder = truth._ENABLED, truth.RECORDER
        try:
            truth._ENABLED = True
            truth.RECORDER = truth.Recorder(); truth.RECORDER.enabled = True
            exporter = TruthExporter()
            events = [{"timestamp_utc":"2026-09-23T16:00:00Z","monotonic_sequence":1}]
            exporter._post_sink = AsyncMock(return_value=False)
            asyncio.run(exporter._export_events(object(), events))
            self.assertEqual(truth.RECORDER.export_dropped_total, 1)
            # No exception propagates to any caller/trading path.
        finally:
            truth._ENABLED, truth.RECORDER = old_enabled, old_recorder


if __name__ == "__main__":
    unittest.main()
