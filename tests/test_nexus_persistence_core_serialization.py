import asyncio
from pathlib import Path


class _FakePgConnection:
    def __init__(self):
        self.active = 0
        self.max_active = 0

    async def _enter(self):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)

    def _leave(self):
        self.active -= 1

    async def execute(self, *args):
        await self._enter()
        try:
            return "OK"
        finally:
            self._leave()

    async def fetch(self, *args):
        await self._enter()
        try:
            return [(1,)]
        finally:
            self._leave()


def test_core_uses_distinct_init_and_io_locks():
    from bot import nexus_persistence as np

    assert np._lock is not np._io_lock


def test_concurrent_reads_and_writes_share_one_core_io_lane():
    from bot import nexus_persistence as np

    async def scenario():
        old_conn = np._conn
        old_is_pg = np._is_pg
        old_io_lock = np._io_lock
        fake = _FakePgConnection()
        try:
            np._conn = fake
            np._is_pg = True
            # Bind a fresh lock to this test loop. Production also uses one
            # event loop, while this keeps the offline suite deterministic.
            np._io_lock = asyncio.Lock()
            tasks = []
            for i in range(4):
                tasks.append(asyncio.create_task(np._execute("UPDATE x SET y=?", (i,))))
                tasks.append(asyncio.create_task(np._fetchall("SELECT ?", (i,))))
            results = await asyncio.gather(*tasks)
            assert fake.max_active == 1
            assert sum(1 for value in results if value == [(1,)]) == 4
        finally:
            np._conn = old_conn
            np._is_pg = old_is_pg
            np._io_lock = old_io_lock

    asyncio.run(scenario())


def test_runtime_overlay_no_longer_replaces_persistence_io_functions():
    root = Path(__file__).resolve().parents[1]
    text = (root / "bot" / "runtime_overlays.py").read_text(encoding="utf-8")
    assert "np._execute =" not in text
    assert "np._fetchall =" not in text
    assert "_single_conn_serialized" not in text
