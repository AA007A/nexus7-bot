import asyncio
import unittest
from unittest.mock import patch

import aiohttp
import websockets

from bot import runtime_truth as truth
from bot import runtime_truth_hooks as hooks
from bot import runtime_truth_rest as truth_rest


class RuntimeTruthProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.old_enabled = truth._ENABLED
        self.old_recorder = truth.RECORDER
        truth._ENABLED = True
        truth.RECORDER = truth.Recorder()
        truth.RECORDER.enabled = True
        hooks._PREVIOUS_WS_SESSION = None

    def tearDown(self):
        truth._ENABLED = self.old_enabled
        truth.RECORDER = self.old_recorder

    def test_rest_seed_provenance_is_sidecar_and_scoped_to_returned_rows(self):
        rows = [
            {"ts": 1000, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 10.0},
            {"ts": 2000, "o": 1.5, "h": 2.5, "l": 1.0, "c": 2.0, "v": 20.0},
        ]
        originals = [dict(r) for r in rows]
        hooks._register_series_provenance("LTCUSDT", "15", rows, "STARTUP_SEED", "raw-rest-1")
        prov = truth.RECORDER.provenance_for("LTCUSDT", "15", rows)
        self.assertEqual([p["source"] for p in prov], ["STARTUP_SEED", "STARTUP_SEED"])
        self.assertEqual([p["raw_event_id"] for p in prov], ["raw-rest-1", "raw-rest-1"])
        self.assertEqual(rows, originals)

    def test_rest_raw_row5_row6_fields_are_preserved_per_candle(self):
        raw = (
            b'{"code":"200000","data":['
            b'[1790178300000,"65","66","63","64","28469","169529.255"],'
            b'[1790177400000,"66","67","64","65","19434","117511.153"]]}'
        )
        fields = truth_rest.remember_raw_kline_fields(raw)
        self.assertEqual(fields[1790178300000]["rest_row_5_volume"], "28469")
        self.assertEqual(fields[1790178300000]["rest_row_6_turnover"], "169529.255")
        self.assertEqual(fields[1790177400000]["rest_row_5_volume"], "19434")
        self.assertEqual(fields[1790177400000]["rest_row_6_turnover"], "117511.153")
        self.assertEqual(truth_rest.current_raw_kline_fields(), fields)

    def test_ws_raw_volume_amount_mapping_and_mutation_provenance(self):
        msg = {
            "type": "message",
            "topic": "/contractMarket/limitCandle:LTCUSDTM_15min",
            "data": {"candles": ["1790178300", "65", "64", "66", "63", "28469", "169529.255"]},
        }
        symbol, timeframe, candle_ts, raw_fields = hooks._raw_ws_fields(msg)
        self.assertEqual(symbol, "LTCUSDT")
        self.assertEqual(timeframe, "15")
        self.assertEqual(candle_ts, 1790178300000)
        self.assertEqual(raw_fields["ws_index_5_volume"], "28469")
        self.assertEqual(raw_fields["ws_index_6_amount"], "169529.255")
        before = []
        row = {"ts": candle_ts, "o": 65.0, "h": 66.0, "l": 63.0, "c": 64.0, "v": 169529.255}
        truth.capture_cache_mutation(
            symbol=symbol, timeframe=timeframe, before_rows=before, after_rows=[row],
            mutation_action="APPEND", source="WS_UPDATE", raw_event_id="ws-raw-7",
            normalized_before=None, normalized_after=row, raw_volume_fields=raw_fields,
        )
        prov = truth.RECORDER.provenance_for(symbol, timeframe, [row])[0]
        self.assertEqual(prov["source"], "WS_UPDATE")
        self.assertEqual(prov["raw_event_id"], "ws-raw-7")
        self.assertEqual(prov["raw_volume_fields"], raw_fields)
        self.assertEqual(prov["normalized_activity"], 169529.255)

    def test_reconnect_event_is_emitted_without_ws_token(self):
        class FakeWS:
            async def __anext__(self):
                raise StopAsyncIteration

        class FakeConnectContext:
            async def __aenter__(self):
                return FakeWS()
            async def __aexit__(self, *args):
                return False

        class FakeClient:
            def __init__(self):
                self._kline_cache = {}
            async def _seed_kline_cache(self, symbols, intervals):
                return None
            async def get_klines(self, symbol, interval, limit=200):
                return []
            async def _handle_ws_message(self, msg):
                return None

        original_ws_connect = websockets.connect
        original_json = aiohttp.ClientResponse.json
        try:
            with patch.object(websockets, "connect", side_effect=lambda *a, **k: FakeConnectContext()):
                hooks.install_transport_and_cache(FakeClient)
                async def exercise():
                    # URI contains a token; telemetry must identify the session without storing the URI/token.
                    async with websockets.connect("wss://example.invalid/endpoint?token=SUPERSECRET&connectId=bgx-123"):
                        pass
                asyncio.run(exercise())
            events = truth.RECORDER.drain(20, 1_000_000)
            reconnect = next(e for e in events if e["event_type"] == "MARKET_WS_RECONNECT")
            text = str(reconnect)
            self.assertIn("new_connection_session_id", text)
            self.assertNotIn("SUPERSECRET", text)
            self.assertNotIn("token=", text)
        finally:
            websockets.connect = original_ws_connect
            aiohttp.ClientResponse.json = original_json


if __name__ == "__main__":
    unittest.main()
