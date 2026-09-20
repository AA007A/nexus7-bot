import unittest
from unittest.mock import AsyncMock, patch
from bot import critical_state


class CriticalStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_failure_is_typed_fail_closed(self):
        with patch.object(critical_state.db, "load_key_value", AsyncMock(side_effect=RuntimeError("db"))):
            with self.assertRaises(critical_state.CriticalStateUnavailable):
                await critical_state.critical_state.load("intent")

    async def test_write_false_is_never_valid_critical_state(self):
        with patch.object(critical_state.db, "save_key_value", AsyncMock(return_value=False)):
            with self.assertRaises(critical_state.CriticalStateUnavailable):
                await critical_state.critical_state.save("intent", "x")

    def test_unavailable_database_blocks_new_risk(self):
        with patch.object(critical_state.db, "_conn", None), patch.object(
            critical_state.db, "configured_postgres_unavailable", return_value=True
        ):
            with self.assertRaises(critical_state.CriticalStateUnavailable):
                critical_state.critical_state.assert_available_for_new_risk()

    def test_reduce_existing_risk_does_not_require_new_risk_repository(self):
        # CriticalStateRepository is intentionally an OPEN_NEW_RISK gate only;
        # emergency protection/close/reduceOnly must remain exchange-available.
        self.assertFalse(hasattr(critical_state.critical_state, "authorize_reduce_existing_risk"))


if __name__ == "__main__":
    unittest.main()
