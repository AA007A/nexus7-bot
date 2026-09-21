import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import execution_ownership as ownership


class BlueGreenOwnershipHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_held_lease_retries_fail_closed_then_acquires(self):
        engine = SimpleNamespace(
            _running=True,
            _execution_ownership_valid=False,
            _execution_ownership_expires_at=None,
        )
        acquired = object()
        attempts = [
            ownership.ExecutionOwnershipUnavailable("LIVE_EXECUTION_OWNERSHIP_HELD"),
            acquired,
        ]

        async def initialize(_engine):
            item = attempts.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        async def no_wait(_seconds):
            return None

        with patch.object(
            ownership,
            "initialize_live_execution_ownership",
            AsyncMock(side_effect=initialize),
        ) as init, patch.object(ownership.asyncio if hasattr(ownership, "asyncio") else asyncio, "sleep", no_wait):
            result = await ownership.wait_for_live_execution_ownership(
                engine,
                retry_seconds=0.1,
            )

        self.assertIs(result, acquired)
        self.assertEqual(init.await_count, 2)
        self.assertFalse(engine._execution_ownership_valid)

    async def test_non_contention_ownership_failure_is_not_hidden(self):
        engine = SimpleNamespace(
            _running=True,
            _execution_ownership_valid=False,
            _execution_ownership_expires_at=None,
        )
        with patch.object(
            ownership,
            "initialize_live_execution_ownership",
            AsyncMock(
                side_effect=ownership.ExecutionOwnershipUnavailable(
                    "PostgreSQL ownership authority unavailable"
                )
            ),
        ):
            with self.assertRaises(ownership.ExecutionOwnershipUnavailable):
                await ownership.wait_for_live_execution_ownership(engine)

    async def test_stopped_engine_never_claims_ownership(self):
        engine = SimpleNamespace(
            _running=False,
            _execution_ownership_valid=False,
            _execution_ownership_expires_at=None,
        )
        with patch.object(
            ownership,
            "initialize_live_execution_ownership",
            AsyncMock(),
        ) as init:
            result = await ownership.wait_for_live_execution_ownership(engine)
        self.assertIsNone(result)
        init.assert_not_awaited()
        self.assertFalse(engine._execution_ownership_valid)


if __name__ == "__main__":
    unittest.main()
