import inspect
import unittest
from unittest.mock import AsyncMock, patch

from bot import private_ws_readonly_observability as obs
from bot import validation_safety_lock


class _Log:
    def __init__(self):
        self.records = []

    def info(self, *args):
        self.records.append(("info", args))

    def warning(self, *args):
        self.records.append(("warning", args))


class _Client:
    def __init__(self, positions=None, orders=None):
        self._positions = positions or []
        self._orders = orders or []

    async def get_positions(self):
        return self._positions

    async def _get(self, path, params=None, auth=False):
        assert path == "/api/v1/orders"
        assert params == {"status": "active"}
        assert auth is True
        return {"items": self._orders}


class PrivateWsReadonlyObservabilityTests(unittest.IsolatedAsyncioTestCase):
    def test_module_contains_no_exchange_mutation_calls(self):
        src = inspect.getsource(obs)
        forbidden = (
            "place_order(", "cancel_order(", "cancel_all_orders(",
            "set_leverage(", "set_position_stops(", "set_sl(",
            "reduceOnly", "close_position(",
        )
        for token in forbidden:
            self.assertNotIn(token, src)

    def test_probe_is_diagnostic_only(self):
        src = inspect.getsource(obs.run)
        self.assertIn("_prelive_private_ws_probe_ok", src)
        self.assertIn("_prelive_account_exposure_clear", src)
        self.assertIn("execution_effect=NONE", src)
        self.assertNotIn("can_open_pilot", src)
        self.assertNotIn("release_approved", src)

    async def test_clear_account_and_private_ws_pass(self):
        client = _Client()
        log = _Log()
        with patch(
            "bot.prelive_readonly_probe._private_ws_probe",
            new=AsyncMock(return_value=True),
        ):
            ok = await obs.run(client, {"ETHUSDT": {}}, log)
        self.assertTrue(ok)
        self.assertTrue(client._prelive_account_exposure_verified)
        self.assertTrue(client._prelive_account_exposure_clear)
        self.assertEqual(client._prelive_active_positions, 0)
        self.assertEqual(client._prelive_active_orders, 0)
        self.assertTrue(client._prelive_private_ws_probe_ok)

    async def test_external_position_blocks_readiness(self):
        client = _Client(positions=[{"symbol": "ETHUSDT", "size": "1", "stopLoss": "100"}])
        log = _Log()
        with patch(
            "bot.prelive_readonly_probe._private_ws_probe",
            new=AsyncMock(return_value=True),
        ):
            ok = await obs.run(client, {"ETHUSDT": {}}, log)
        self.assertFalse(ok)
        self.assertTrue(client._prelive_account_exposure_verified)
        self.assertFalse(client._prelive_account_exposure_clear)
        self.assertEqual(client._prelive_active_positions, 1)

    async def test_active_order_blocks_readiness_and_logs_forensics(self):
        client = _Client(orders=[{
            "id": "o1",
            "clientOid": "manual-123",
            "symbol": "ADAUSDTM",
            "side": "buy",
            "type": "limit",
            "status": "active",
            "price": "0.41",
            "size": "12",
            "dealSize": "0",
            "createdAt": 1788890000000,
        }])
        log = _Log()
        with patch(
            "bot.prelive_readonly_probe._private_ws_probe",
            new=AsyncMock(return_value=True),
        ):
            ok = await obs.run(client, {"ETHUSDT": {}}, log)
        self.assertFalse(ok)
        self.assertFalse(client._prelive_account_exposure_clear)
        self.assertEqual(client._prelive_active_orders, 1)
        forensic = [args for level, args in log.records if "[PRELIVE_ACTIVE_ORDER]" in str(args)]
        self.assertEqual(len(forensic), 1)
        rendered = str(forensic[0])
        self.assertIn("ADAUSDTM", rendered)
        self.assertIn("manual-123", rendered)
        self.assertIn("o1", rendered)
        self.assertIn("read_only=true", rendered)
        self.assertIn("execution_effect=NONE", rendered)

    def test_validation_lock_blocks_shadow_candidate_until_readonly_ready(self):
        src = inspect.getsource(validation_safety_lock.install)
        self.assertIn("_shadow_prelive_readonly_ready", src)
        self.assertIn("stage=ACCOUNT_EXPOSURE", src)
        self.assertIn("execution_effect=NONE", src)


if __name__ == "__main__":
    unittest.main()
