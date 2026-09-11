import asyncio
from types import SimpleNamespace

from bot import pilot_external_position_guard as guard


class DummyLog:
    def __getattr__(self, _):
        return lambda *a, **k: None


class DummyClient:
    def __init__(self, rows):
        self.rows = rows
        self.mutations = []

    async def get_positions(self):
        return list(self.rows)


class Engine:
    def __init__(self, rows):
        self.paper_trade = False
        self.client = DummyClient(rows)
        self.positions = {}
        self._trade_ids = {}
        self._unprotected_symbols = set()

    async def _load_existing_positions(self):
        # Reproduce the legacy bug: blindly auto-adopt every exchange position.
        for row in await self.client.get_positions():
            if float(row.get("size", 0) or 0) > 0:
                self.positions[row["symbol"]] = SimpleNamespace(symbol=row["symbol"])
                self._trade_ids[row["symbol"]] = 999

    async def _guard_naked_positions(self):
        self.client.mutations.append("guard")

    async def _sync_positions(self):
        self.client.mutations.append("sync")

    async def _reconcile_exchange_positions(self, only_symbol=None):
        self.client.mutations.append(("reconcile", only_symbol))


def _row(symbol="NEARUSDT"):
    return {
        "symbol": symbol,
        "size": 10,
        "side": "Buy",
        "entryPrice": 2.5,
        "markPrice": 2.5,
        "stopLoss": 0,
    }


def test_startup_manual_position_is_not_adopted(monkeypatch):
    async def unprotected(*_args, **_kwargs):
        return False, "no_full_protective_stop"
    monkeypatch.setattr(guard, "conditional_stop_confirmed", unprotected)
    guard.install(Engine, DummyLog())
    e = Engine([_row()])
    asyncio.run(e._load_existing_positions())
    assert "NEARUSDT" in e._external_position_symbols
    assert "NEARUSDT" not in e.positions
    assert "NEARUSDT" not in e._trade_ids


def test_external_position_never_reaches_naked_guard(monkeypatch):
    async def unprotected(*_args, **_kwargs):
        return False, "no_full_protective_stop"
    monkeypatch.setattr(guard, "conditional_stop_confirmed", unprotected)
    e = Engine([_row()])
    asyncio.run(e._load_existing_positions())
    asyncio.run(e._guard_naked_positions())
    assert e.client.mutations == []


def test_external_symbol_cannot_use_scoped_reconcile(monkeypatch):
    async def unprotected(*_args, **_kwargs):
        return False, "no_full_protective_stop"
    monkeypatch.setattr(guard, "conditional_stop_confirmed", unprotected)
    e = Engine([_row()])
    asyncio.run(e._load_existing_positions())
    asyncio.run(e._reconcile_exchange_positions(only_symbol="NEARUSDT"))
    assert e.client.mutations == []
