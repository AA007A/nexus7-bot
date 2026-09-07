import asyncio
from datetime import datetime, timedelta, timezone
import unittest

from bot import stagnation_time_hardening as sth


class _Log:
    def info(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        raise AssertionError(f"unexpected log.error: {args}")


class _Client:
    def __init__(self):
        self.orders = []
        self.k15 = [
            {"c": 100.0, "h": 101.0, "l": 99.0}
            for _ in range(100)
        ]

    def get_cached_klines(self, symbol, interval, limit=100):
        if interval == "15":
            return list(self.k15)
        return []

    async def place_order(self, **kwargs):
        self.orders.append(kwargs)
        return {"orderId": "paper-test"}


class _Position:
    direction = "LONG"
    entry = 100.0
    current_price = 100.0
    qty = 1.0
    tp1_hit = False

    def __init__(self, opened_at):
        self.opened_at = opened_at


class _Engine:
    def __init__(self, opened_at):
        self.client = _Client()
        self.positions = {"TESTUSDT": _Position(opened_at)}
        self.instruments = {"TESTUSDT": {}}


class StagnationTimeHardeningTests(unittest.TestCase):
    def test_bars_since_open_uses_elapsed_time(self):
        opened = datetime(2026, 1, 1, tzinfo=timezone.utc)
        now = opened.timestamp() + (15 * 60 * 16) + 1
        self.assertEqual(sth.bars_since_open(opened, now_ts=now), 16)

    def test_fresh_position_not_closed_even_with_100_cached_bars(self):
        class Engine(_Engine):
            pass

        sth.install(Engine, _Log())
        eng = Engine(datetime.now(timezone.utc))
        asyncio.run(eng._check_stagnation_and_invalidation())
        self.assertEqual(eng.client.orders, [])

    def test_position_older_than_4h_can_close_for_stagnation(self):
        class Engine(_Engine):
            pass

        sth.install(Engine, _Log())
        opened = datetime.now(timezone.utc) - timedelta(hours=4, minutes=1)
        eng = Engine(opened)
        asyncio.run(eng._check_stagnation_and_invalidation())
        self.assertEqual(len(eng.client.orders), 1)
        self.assertTrue(eng.client.orders[0]["reduce_only"])


if __name__ == "__main__":
    unittest.main()
