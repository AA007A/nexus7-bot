import asyncio
import json
import unittest

import aiosqlite

from bot import database as db


class DatabaseAtomicPaperTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_conn = db._conn
        self.original_is_pg = db._is_pg
        db._conn = await aiosqlite.connect(":memory:")
        db._is_pg = False
        await db._create_tables()

    async def asyncTearDown(self):
        await db._conn.close()
        db._conn = self.original_conn
        db._is_pg = self.original_is_pg

    async def test_open_and_close_commit_trade_with_runtime_snapshot(self):
        def opened_state(trade_id):
            return json.dumps({"phase": "open", "trade_id": trade_id})

        trade_id = await db.save_paper_open_atomic(
            "BTCUSDT", "Buy", 100.0, 0.25, 50, 80,
            "paper_runtime_state_v1", opened_state,
            sl=98.0, direction="LONG",
        )
        self.assertGreater(trade_id, 0)
        row = await db._fetchone(
            "SELECT status FROM trades WHERE id=?", (trade_id,), strict=True
        )
        self.assertEqual(row[0], "open")
        self.assertEqual(
            json.loads(await db.load_key_value("paper_runtime_state_v1"))["phase"],
            "open",
        )

        closed_state = json.dumps({"phase": "closed", "trade_id": trade_id})
        await db.save_paper_close_atomic(
            trade_id, 104.0, 0.95, 0.05, 60.0, "PAPER_TP",
            "paper_runtime_state_v1", closed_state,
        )
        row = await db._fetchone(
            "SELECT status,pnl FROM trades WHERE id=?", (trade_id,), strict=True
        )
        self.assertEqual(row[0], "closed")
        self.assertAlmostEqual(row[1], 0.95)
        self.assertEqual(
            json.loads(await db.load_key_value("paper_runtime_state_v1"))["phase"],
            "closed",
        )
        performance = await db._fetchone(
            "SELECT total_trades FROM performance ORDER BY id DESC LIMIT 1",
            strict=True,
        )
        self.assertEqual(performance[0], 1)

    async def test_postgres_open_uses_returning_without_select_max(self):
        class FakePostgres:
            def __init__(self):
                self.calls = []

            async def fetchrow(self, sql, *params):
                self.calls.append((sql, params))
                return (123,)

        sqlite_conn = db._conn
        fake = FakePostgres()
        db._conn = fake
        db._is_pg = True
        try:
            trade_id = await db.save_trade_open(
                "BTCUSDT", "Buy", 100.0, 0.25, 50, 80,
                sl=98.0, direction="LONG",
            )
        finally:
            db._conn = sqlite_conn
            db._is_pg = False

        self.assertEqual(trade_id, 123)
        self.assertEqual(len(fake.calls), 1)
        sql = fake.calls[0][0].upper()
        self.assertIn("INSERT INTO TRADES", sql)
        self.assertIn("RETURNING ID", sql)
        self.assertNotIn("SELECT MAX", sql)

    async def test_concurrent_open_ids_are_unique(self):
        first, second = await asyncio.gather(
            db.save_trade_open(
                "BTCUSDT", "Buy", 100.0, 0.25, 50, 80,
                sl=98.0, direction="LONG",
            ),
            db.save_trade_open(
                "ETHUSDT", "Sell", 100.0, 0.25, 50, 80,
                sl=102.0, direction="SHORT",
            ),
        )
        self.assertNotEqual(first, second)
        rows = await db._fetchall("SELECT id FROM trades ORDER BY id")
        self.assertEqual([row[0] for row in rows], [first, second])

    async def test_open_rolls_back_when_snapshot_build_fails(self):
        def invalid_state(_trade_id):
            raise ValueError("snapshot rejected")

        with self.assertRaises(db.PersistenceError):
            await db.save_paper_open_atomic(
                "ETHUSDT", "Buy", 100.0, 0.25, 50, 80,
                "paper_runtime_state_v1", invalid_state,
                sl=98.0, direction="LONG",
            )
        row = await db._fetchone("SELECT COUNT(*) FROM trades", strict=True)
        self.assertEqual(row[0], 0)

    async def test_close_rolls_back_when_trade_is_not_open(self):
        trade_id = await db.save_paper_open_atomic(
            "SOLUSDT", "Buy", 100.0, 0.25, 50, 80,
            "paper_runtime_state_v1",
            lambda new_id: json.dumps({"phase": "open", "trade_id": new_id}),
            sl=98.0, direction="LONG",
        )
        await db.save_paper_close_atomic(
            trade_id, 104.0, 0.95, 0.05, 60.0, "PAPER_TP",
            "paper_runtime_state_v1", json.dumps({"phase": "closed"}),
        )
        with self.assertRaises(db.PersistenceError):
            await db.save_paper_close_atomic(
                trade_id, 105.0, 1.2, 0.05, 61.0, "PAPER_TP",
                "paper_runtime_state_v1", json.dumps({"phase": "closed_twice"}),
            )
        state = json.loads(await db.load_key_value("paper_runtime_state_v1"))
        self.assertEqual(state["phase"], "closed")


if __name__ == "__main__":
    unittest.main()
