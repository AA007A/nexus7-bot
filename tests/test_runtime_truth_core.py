import os
import unittest
from unittest.mock import patch

from bot import runtime_truth as truth


class RuntimeTruthCoreTests(unittest.TestCase):
    def setUp(self):
        self.old_enabled = truth._ENABLED
        self.old_recorder = truth.RECORDER

    def tearDown(self):
        truth._ENABLED = self.old_enabled
        truth.RECORDER = self.old_recorder

    def _enabled_recorder(self, *, max_events=64, max_bytes=1_000_000, max_event_bytes=100_000):
        truth._ENABLED = True
        with patch.object(truth, "_MAX_EVENTS", max_events), patch.object(truth, "_MAX_BYTES", max_bytes), patch.object(truth, "_MAX_EVENT_BYTES", max_event_bytes):
            recorder = truth.Recorder()
        truth.RECORDER = recorder
        return recorder

    def test_feature_flag_default_is_false(self):
        self.assertEqual(os.environ.get("BGX_RUNTIME_TRUTH_ENABLED", "false").strip().lower(), "false")

    def test_disabled_emit_is_noop_and_does_not_advance_sequence(self):
        truth._ENABLED = False
        recorder = truth.Recorder()
        self.assertFalse(recorder.enabled)
        self.assertIsNone(recorder.emit("MARKET_TEST", payload={"x": 1}))
        self.assertEqual(recorder.current_sequence, 0)
        self.assertEqual(recorder.drain(10, 10000), [])

    def test_sequence_increments_before_queue_full_drop(self):
        recorder = self._enabled_recorder(max_events=1)
        first = recorder.emit("MARKET_TEST", payload={"n": 1})
        second = recorder.emit("MARKET_TEST", payload={"n": 2})
        self.assertTrue(first.endswith(":1"))
        self.assertTrue(second.endswith(":2"))
        self.assertEqual(recorder.current_sequence, 2)
        self.assertEqual(recorder.telemetry_dropped_total, 1)
        drained = recorder.drain(10, 10000)
        self.assertEqual([e["monotonic_sequence"] for e in drained], [1])

    def test_queue_byte_limit_is_enforced(self):
        recorder = self._enabled_recorder(max_events=100, max_bytes=1500, max_event_bytes=1000)
        for i in range(20):
            recorder.emit("MARKET_TEST", payload={"blob": "x" * 500, "i": i})
        metrics = recorder.metrics()
        self.assertLessEqual(metrics["queue_bytes"], recorder.max_bytes)
        self.assertGreater(recorder.telemetry_dropped_total, 0)

    def test_oversized_event_drops_fail_open(self):
        recorder = self._enabled_recorder(max_event_bytes=700)
        event_id = recorder.emit("MARKET_TEST", payload={"blob": "x" * 5000})
        self.assertIsNotNone(event_id)
        self.assertEqual(recorder.oversized_total, 1)
        self.assertEqual(recorder.current_sequence, 1)
        self.assertEqual(recorder.drain(10, 10000), [])

    def test_cache_hash_v1_is_deterministic_and_chronological(self):
        rows = [
            {"ts": 2, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 11.0},
            {"ts": 1, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.4, "v": 10.0},
        ]
        first = truth.cache_data_hash(rows)
        second = truth.cache_data_hash(list(reversed(rows)))
        self.assertEqual(first, second)
        changed = [dict(r) for r in rows]
        changed[0]["v"] = 11.000000000000002
        self.assertNotEqual(first, truth.cache_data_hash(changed))
        self.assertEqual(truth.HASH_VERSION, "BGX_CACHE_HASH_V1")

    def test_provenance_is_sidecar_only(self):
        recorder = self._enabled_recorder()
        row = {"ts": 10, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 7.0}
        original = dict(row)
        recorder.register_provenance("LTCUSDT", "15", 10, {"source": "WS_UPDATE", "candle_ts": 10})
        self.assertEqual(row, original)
        self.assertEqual(recorder.provenance_for("LTCUSDT", "15", [row])[0]["source"], "WS_UPDATE")

    def test_rest_capture_records_raw_body_not_second_request(self):
        self._enabled_recorder()
        with truth.rest_purpose("STARTUP_SEED"):
            event_id = truth.capture_rest_response(
                "/api/v1/kline/query",
                {"symbol": "LTCUSDTM", "granularity": "15", "from": "1", "to": "2"},
                200,
                b'{"code":"200000","data":[[1,"1","2","0.5","1.5","10","20"]]}',
            )
        self.assertIsNotNone(event_id)
        event = truth.RECORDER.drain(10, 100000)[0]
        self.assertEqual(event["event_type"], "MARKET_REST_SEED")
        self.assertEqual(event["payload"]["purpose"], "STARTUP_SEED")
        self.assertIn('"200000"', event["payload"]["raw_response_utf8"])

    def test_application_ws_payload_is_exact_utf8_and_not_network_frame_claim(self):
        self._enabled_recorder()
        payload = '{"type":"message","topic":"/contractMarket/limitCandle:LTCUSDTM_15min","data":{"candles":["1","2","3","4","1","10","20"]}}'
        truth.capture_ws_application_payload(payload, "session-1")
        event = truth.RECORDER.drain(10, 100000)[0]
        self.assertEqual(event["event_type"], "MARKET_WS_RAW")
        self.assertEqual(event["payload"]["payload"], payload)
        self.assertEqual(event["payload"]["payload_kind"], "APPLICATION_WS_PAYLOAD_UTF8")
        self.assertNotIn("RAW_NETWORK_FRAME", str(event))


if __name__ == "__main__":
    unittest.main()
