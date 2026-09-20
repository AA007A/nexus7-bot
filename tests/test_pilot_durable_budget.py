import os
import unittest
from unittest.mock import AsyncMock, PropertyMock, patch

from bot.pilot import PilotGuard


class DurablePilotGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_intent_is_delegated_to_durable_atomic_reservation(self):
        guard = PilotGuard()
        with patch.object(type(guard), "enabled", new_callable=PropertyMock, return_value=True), \
             patch.dict(os.environ, {"PILOT_SESSION_ID": "pilot-20260920"}, clear=False), \
             patch("bot.pilot.db.reserve_pilot_submission_atomic", AsyncMock(return_value=(True, 1))) as reserve:
            self.assertTrue(await guard.reserve_submission("BTCUSDT", "intent-1"))
            reserve.assert_awaited_once_with("pilot-20260920", "intent-1", 2)

    async def test_database_failure_blocks_submission(self):
        guard = PilotGuard()
        from bot import database as db
        with patch.object(type(guard), "enabled", new_callable=PropertyMock, return_value=True), \
             patch.dict(os.environ, {"PILOT_SESSION_ID": "pilot-20260920"}, clear=False), \
             patch("bot.pilot.db.reserve_pilot_submission_atomic", AsyncMock(side_effect=db.PersistenceError("down"))):
            self.assertFalse(await guard.reserve_submission("BTCUSDT", "intent-2"))

    async def test_missing_session_id_fails_closed(self):
        guard = PilotGuard()
        env = dict(os.environ)
        env.pop("PILOT_SESSION_ID", None)
        with patch.object(type(guard), "enabled", new_callable=PropertyMock, return_value=True), \
             patch.dict(os.environ, env, clear=True):
            self.assertFalse(await guard.reserve_submission("BTCUSDT", "intent-3"))


if __name__ == "__main__":
    unittest.main()
