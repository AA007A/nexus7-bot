import unittest
from types import SimpleNamespace

from bot import kucoin_fill_normalization as hardening


class _Log:
    warning = staticmethod(lambda *a, **k: None)
    info = staticmethod(lambda *a, **k: None)
    error = staticmethod(lambda *a, **k: None)


class _FakeClient:
    def __init__(self, status, instruments):
        self._status = status
        self._instruments = instruments

    async def wait_for_fill(self, order_id, *args, **kwargs):
        return {"filled": True, "status": dict(self._status), "timed_out": False}


class FillNormalizationTests(unittest.IsolatedAsyncioTestCase):
    def _module(self):
        return SimpleNamespace(
            to_standard=lambda symbol: {
                "XRPUSDTM": "XRPUSDT",
                "XBTUSDTM": "BTCUSDT",
            }.get(symbol, symbol)
        )

    async def test_xrp_contract_multiplier_restores_real_asset_price(self):
        class Client(_FakeClient):
            pass
        hardening.install(Client, self._module(), _Log())
        client = Client(
            {"symbol": "XRPUSDTM", "dealSize": "1", "dealValue": "13.3703", "filledSize": "1"},
            {"XRPUSDT": {"multiplier": 10.0}},
        )
        out = await client.wait_for_fill("kc-xrp")
        st = out["status"]
        self.assertEqual(st["dealSizeContracts"], 1.0)
        self.assertEqual(st["dealSize"], 10.0)
        self.assertAlmostEqual(st["avgDealPriceNormalized"], 1.33703, places=8)
        # Engine's existing dealValue/dealSize formula must now yield spot-like price.
        self.assertAlmostEqual(float(st["dealValue"]) / float(st["dealSize"]), 1.33703, places=8)
        self.assertEqual(st["filledSize"], "1")

    async def test_btc_fractional_multiplier_restores_real_asset_price(self):
        class Client(_FakeClient):
            pass
        hardening.install(Client, self._module(), _Log())
        client = Client(
            {"symbol": "XBTUSDTM", "dealSize": "1", "dealValue": "100.5", "filledSize": "1"},
            {"BTCUSDT": {"multiplier": 0.001}},
        )
        out = await client.wait_for_fill("kc-btc")
        st = out["status"]
        self.assertAlmostEqual(st["dealSize"], 0.001)
        self.assertAlmostEqual(st["avgDealPriceNormalized"], 100500.0)

    async def test_missing_multiplier_keeps_exchange_status_unchanged(self):
        class Client(_FakeClient):
            pass
        hardening.install(Client, self._module(), _Log())
        original = {"symbol": "XRPUSDTM", "dealSize": "1", "dealValue": "13.3703", "filledSize": "1"}
        client = Client(original, {})
        out = await client.wait_for_fill("kc-xrp")
        self.assertEqual(out["status"], original)


if __name__ == "__main__":
    unittest.main()
