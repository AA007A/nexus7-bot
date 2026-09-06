import unittest
from unittest.mock import patch

from bot import database


class FailingRollbackConn:
    async def execute(self, *args, **kwargs):
        raise RuntimeError("write boom")

    async def rollback(self):
        raise RuntimeError("rollback boom")


class DatabaseRollbackObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_trade_open_rollback_failure_does_not_mask_primary_error(self):
        with patch.object(database, "_conn", FailingRollbackConn()), \
             patch.object(database, "_is_pg", False), \
             patch.object(database.log, "error") as log_error:
            with self.assertRaises(database.PersistenceError) as excinfo:
                await database.save_trade_open("BTCUSDT", "LONG", 100.0, 1.0, 2, 60)

        self.assertIn("trade open insert failed", str(excinfo.exception))
        self.assertTrue(
            any("rollback failed" in str(call) for call in log_error.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
