import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from bot import atomic_key_value as akv
from bot import database as db


class _Tx:
    async def __aenter__(self): return self
    async def __aexit__(self, exc_type, exc, tb): return False


class AtomicKeyValueTests(unittest.IsolatedAsyncioTestCase):
    async def test_postgres_uses_single_transaction_for_all_keys(self):
        conn = MagicMock()
        conn.transaction.return_value = _Tx()
        conn.execute = AsyncMock(return_value="INSERT 0 1")
        with patch.object(db, "_conn", conn), patch.object(db, "_is_pg", True):
            ok = await akv.save_key_values_atomic((("a", "1"), ("b", "2")), strict=True)
        self.assertTrue(ok)
        self.assertEqual(conn.execute.await_count, 2)
        conn.transaction.assert_called_once()

    async def test_sqlite_commits_only_after_all_writes(self):
        conn = MagicMock()
        conn.execute = AsyncMock(return_value=None)
        conn.commit = AsyncMock(return_value=None)
        conn.rollback = AsyncMock(return_value=None)
        with patch.object(db, "_conn", conn), patch.object(db, "_is_pg", False):
            ok = await akv.save_key_values_atomic((("a", "1"), ("b", "2")), strict=True)
        self.assertTrue(ok)
        self.assertEqual(conn.execute.await_count, 3)  # BEGIN + 2 UPSERTs
        conn.commit.assert_awaited_once()
        conn.rollback.assert_not_awaited()

    async def test_sqlite_partial_failure_rolls_back_and_fails_closed(self):
        conn = MagicMock()
        conn.execute = AsyncMock(side_effect=[None, None, RuntimeError("second write failed")])
        conn.commit = AsyncMock(return_value=None)
        conn.rollback = AsyncMock(return_value=None)
        with patch.object(db, "_conn", conn), patch.object(db, "_is_pg", False):
            with self.assertRaises(db.PersistenceError):
                await akv.save_key_values_atomic((("a", "1"), ("b", "2")), strict=True)
        conn.commit.assert_not_awaited()
        conn.rollback.assert_awaited_once()

    async def test_unavailable_database_fails_closed_in_strict_mode(self):
        with patch.object(db, "_conn", None):
            with self.assertRaises(db.PersistenceError):
                await akv.save_key_values_atomic((("a", "1"),), strict=True)

    async def test_rejects_duplicate_keys(self):
        with self.assertRaises(ValueError):
            await akv.save_key_values_atomic((("a", "1"), ("a", "2")), strict=True)


if __name__ == "__main__": unittest.main()
