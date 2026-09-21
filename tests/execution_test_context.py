"""Canonical valid runtime context for adapter-boundary tests.

This fixture does not bypass production gates.  It builds the same engine state
consumed by runtime_readiness and supplies a semantically faithful isolated
ownership authority.  PostgreSQL-specific locking remains covered separately by
the ownership/pilot integration tests.
"""
from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import execution_ownership as eo


class _Tx:
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False


class IsolatedOwnershipConnection:
    def __init__(self):
        self.value = None
    def transaction(self): return _Tx()
    async def execute(self, sql, *args):
        if "INSERT INTO key_value" in sql:
            self.value = args[1]
        return "OK"
    async def fetchrow(self, sql, *args):
        return (self.value,) if self.value else None


class ValidExecutionTestContext:
    def __init__(self, client, instruments=None):
        self.client = client
        self.conn = IsolatedOwnershipConnection()
        instruments = instruments or getattr(client, "_instruments", {}) or {"TESTUSDT": {"multiplier": 1}}
        self.engine = SimpleNamespace(
            instruments=instruments,
            _durable_state_ok=True,
            _financial_state_sane=True,
            _initial_reconciliation_complete=True,
            _execution_ownership_valid=False,
            connected=True,
            _market_data_ready=True,
            viable_symbols=list(instruments),
            _protection_system_ready=True,
            _protection_readiness_receipt={
                "observed_symbols": [],
                "positions": 0,
                "protected_positions": 0,
                "unprotected_positions": 0,
                "readback_complete": True,
                "evidence": {},
            },
            positions={},
            _external_position_symbols=set(),
            _unprotected_symbols=set(),
            client=self.client,
        )
        self.client._engine = self.engine
        self.stack = AsyncExitStack()

    async def __aenter__(self):
        self.stack.enter_context(patch.object(eo.db, "_conn", self.conn))
        self.stack.enter_context(patch.object(eo.db, "_is_pg", True))
        self.stack.enter_context(patch.object(eo.db, "configured_postgres_unavailable", return_value=False))
        self.stack.enter_context(patch("bot.critical_state.db._conn", self.conn))
        self.stack.enter_context(patch("bot.critical_state.db.configured_postgres_unavailable", return_value=False))
        self.stack.enter_context(patch.dict("os.environ", {"EXECUTION_CAPABILITY": "LIVE"}, clear=False))
        return self

    async def __aexit__(self, *args):
        return await self.stack.__aexit__(*args)
