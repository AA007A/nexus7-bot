import asyncio
import unittest
from unittest.mock import patch

import aiohttp
import websockets

from bot import runtime_truth as truth
from bot import runtime_truth_hooks as hooks


class RuntimeTruthWSProxyRegressionTests(unittest.TestCase):
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

    def test_proxy_uses_recv_for_websockets12_protocol_without_dunder_anext(self):
        class WebSockets12LikeProtocol:
            def __init__(self):
                self.messages = [
                    '{"type":"ack","id":"1"}',
                    '{"type":"message","topic":"/contractMarket/limitCandle:LTCUSDTM_15min","data":{"candles":["1790178300","65","64","66","63","28469","169529.255"]}}',
                ]
                self.sent = []

            async def recv(self):
                if not self.messages:
                    raise StopAsyncIteration
                return self.messages.pop(0)

            async def send(self, payload):
                self.sent.append(payload)

        class FakeConnectContext:
            def __init__(self):
                self.ws = WebSockets12LikeProtocol()

            async def __aenter__(self):
                return self.ws

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
                    seen = []
                    async with websockets.connect(
                        "wss://example.invalid/endpoint?token=SUPERSECRET&connectId=bgx-123"
                    ) as ws:
                        async for raw in ws:
                            seen.append(raw)
                    return seen

                seen = asyncio.run(exercise())

            self.assertEqual(len(seen), 2)
            self.assertIn('"type":"ack"', seen[0])
            events = truth.RECORDER.drain(20, 1_000_000)
            raw_events = [e for e in events if e["event_type"] == "MARKET_WS_RAW"]
            self.assertEqual(len(raw_events), 2)
            self.assertNotIn("SUPERSECRET", str(raw_events))
        finally:
            websockets.connect = original_ws_connect
            aiohttp.ClientResponse.json = original_json


if __name__ == "__main__":
    unittest.main()
