import unittest
from unittest.mock import patch

from bot import database


class FailingPG:
    async def fetch(self, *args, **kwargs):
        raise RuntimeError("read boom")

    async def fetchrow(self, *args, **kwargs):
        raise RuntimeError("read boom")


class DatabaseObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetchall_non_strict_is_observable_and_returns_empty(self):
        with patch.object(database, "_conn", FailingPG()), \
             patch.object(database, "_is_pg", True), \
             patch.object(database.log, "warning") as log_warning:
            rows = await database._fetchall("SELECT 1")

        self.assertEqual(rows, [])
        self.assertTrue(
            any("DB fetchall failed" in str(call) for call in log_warning.call_args_list)
        )

    async def test_fetchall_strict_fails_closed(self):
        with patch.object(database, "_conn", FailingPG()), \
             patch.object(database, "_is_pg", True):
            with self.assertRaises(database.PersistenceError):
                await database._fetchall("SELECT 1", strict=True)

    async def test_fetchone_non_strict_is_observable(self):
        with patch.object(database, "_conn", FailingPG()), \
             patch.object(database, "_is_pg", True), \
             patch.object(database.log, "warning") as log_warning:
            row = await database._fetchone("SELECT 1")

        self.assertIsNone(row)
        self.assertTrue(
            any("DB fetchone failed" in str(call) for call in log_warning.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
