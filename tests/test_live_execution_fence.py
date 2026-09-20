import asyncio
import types
import unittest
from unittest import mock

from bot import live_execution_fence as fence


class _Log:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _Conn:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.calls = 0
        self.closed = False

    async def fetchval(self, sql, lock_id):
        self.calls += 1
        assert "pg_try_advisory_lock" in sql
        assert lock_id == fence._ADVISORY_LOCK_ID
        return self.acquired

    def is_closed(self):
        return self.closed


class _DB:
    def __init__(self, conn=None, is_pg=True):
        self._conn = conn
        self._is_pg = is_pg
        self._io_lock = asyncio.Lock()


class LiveExecutionFenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fence._owned_conn = None

    async def test_db_outage_allows_only_verified_reduction(self):
        class Client:
            calls = 0
            async def get_positions(self):
                return [dict(symbol='ETHUSDT', side='Buy', size=.01, sizeUnit='BASE_ASSET')]
            async def place_order(self, *args, **kwargs):
                self.calls += 1
                return 'reduced'
        with mock.patch.object(fence, 'db', _DB(None, False)):
            fence.install(Client, types.SimpleNamespace(PAPER_TRADE=False), _Log())
            c = Client()
            self.assertEqual(await c.place_order(symbol='ETHUSDT', side='Sell', qty=.01, reduce_only=True), 'reduced')
            for extra in [dict(reduce_only=False), dict(reduce_only='true'), dict(side='Buy'), dict(qty=.02)]:
                request=dict(symbol='ETHUSDT', side='Sell', qty=.01, reduce_only=True)
                request.update(extra)
                with self.assertRaises(RuntimeError):
                    await c.place_order(**request)
            self.assertEqual(c.calls,1)

    async def test_reduction_with_unknown_exchange_state_is_blocked(self):
        client=types.SimpleNamespace(get_positions=mock.AsyncMock(side_effect=TimeoutError()))
        self.assertFalse(await fence._verified_reduction(client, (), dict(symbol='ETHUSDT', side='Sell', qty=.01, reduce_only=True)))

    async def test_live_owner_dispatches_and_lock_is_not_reentered(self):
        conn = _Conn(acquired=True)
        fake_db = _DB(conn)
        mode = types.SimpleNamespace(PAPER_TRADE=False)

        class Client:
            def __init__(self):
                self.calls = 0

            async def place_order(self, *args, **kwargs):
                self.calls += 1
                return {"ok": True}

        with mock.patch.object(fence, "db", fake_db):
            fence.install(Client, mode, _Log())
            client = Client()
            self.assertEqual(await client.place_order("BTCUSDT"), {"ok": True})
            self.assertEqual(await client.place_order("ETHUSDT"), {"ok": True})

        self.assertEqual(client.calls, 2)
        self.assertEqual(conn.calls, 1, "session advisory lock must not be re-entered")

    async def test_second_instance_cannot_dispatch_live_order(self):
        conn = _Conn(acquired=False)
        fake_db = _DB(conn)
        mode = types.SimpleNamespace(PAPER_TRADE=False)

        class Client:
            def __init__(self):
                self.calls = 0

            async def place_order(self, *args, **kwargs):
                self.calls += 1
                return {"ok": True}

        with mock.patch.object(fence, "db", fake_db):
            fence.install(Client, mode, _Log())
            client = Client()
            with self.assertRaisesRegex(RuntimeError, "ownership fence"):
                await client.place_order("BTCUSDT")

        self.assertEqual(client.calls, 0)
        self.assertEqual(conn.calls, 1)

    async def test_other_owner_cannot_use_emergency_reduction_exception(self):
        class Client:
            place_order = mock.AsyncMock()
            get_positions = mock.AsyncMock(return_value=[dict(symbol='ETHUSDT', side='Buy', size=.01, sizeUnit='BASE_ASSET')])
        with mock.patch.object(fence, 'db', _DB(_Conn(acquired=False))):
            fence.install(Client, types.SimpleNamespace(PAPER_TRADE=False), _Log())
            with self.assertRaises(RuntimeError):
                await Client().place_order(symbol='ETHUSDT', side='Sell', qty=.01, reduce_only=True)
            Client.get_positions.assert_not_awaited()

    async def test_paper_bypasses_database_fence(self):
        fake_db = _DB(conn=None, is_pg=False)
        mode = types.SimpleNamespace(PAPER_TRADE=True)

        class Client:
            async def place_order(self, *args, **kwargs):
                return "paper-ok"

        with mock.patch.object(fence, "db", fake_db):
            fence.install(Client, mode, _Log())
            self.assertEqual(await Client().place_order("BTCUSDT"), "paper-ok")

    async def test_new_postgres_connection_reacquires_ownership(self):
        first = _Conn(acquired=True)
        second = _Conn(acquired=True)
        fake_db = _DB(first)
        mode = types.SimpleNamespace(PAPER_TRADE=False)

        class Client:
            async def place_order(self, *args, **kwargs):
                return "live-ok"

        with mock.patch.object(fence, "db", fake_db):
            fence.install(Client, mode, _Log())
            client = Client()
            self.assertEqual(await client.place_order("BTCUSDT"), "live-ok")
            fake_db._conn = second
            self.assertEqual(await client.place_order("ETHUSDT"), "live-ok")

        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 1)


if __name__ == "__main__":
    unittest.main()
