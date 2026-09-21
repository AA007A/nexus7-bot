import json, unittest
import asyncio
from types import SimpleNamespace
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from bot import execution_ownership as eo

class Tx:
    async def __aenter__(self): return self
    async def __aexit__(self,*a): return False
class Conn:
    def __init__(self): self.value=None
    def transaction(self): return Tx()
    async def execute(self,sql,*args):
        if "INSERT INTO key_value" in sql: self.value=args[1]
        return "OK"
    async def fetchrow(self,sql,*args):
        return (self.value,) if self.value else None

class OwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn=Conn()
        self.patches=[
          patch.object(eo.db,"_conn",self.conn),patch.object(eo.db,"_is_pg",True),
          patch.object(eo.db,"configured_postgres_unavailable",return_value=False),
          patch.dict("os.environ",{"EXECUTION_CAPABILITY":"LIVE"},clear=False)]
        for p in self.patches:p.start()
    async def asyncTearDown(self):
        for p in reversed(self.patches):p.stop()

    async def test_takeover_monotonically_fences_stale_owner(self):
        a=await eo.acquire_execution_ownership("A")
        state=json.loads(self.conn.value); state["expires_at"]="2000-01-01T00:00:00+00:00"; self.conn.value=json.dumps(state)
        b=await eo.acquire_execution_ownership("B")
        self.assertEqual(b.fencing_token,a.fencing_token+1)
        with self.assertRaises(eo.StaleExecutionFence):
            await eo.validate_execution_ownership(a)
        await eo.validate_execution_ownership(b)


    async def test_normal_renewal_keeps_owner_session_and_token_and_extends_lease(self):
        a=await eo.acquire_execution_ownership("A")
        await asyncio.sleep(0.002)
        renewed=await eo.acquire_execution_ownership("A")
        self.assertEqual(renewed.owner_id,a.owner_id)
        self.assertEqual(renewed.session_id,a.session_id)
        self.assertEqual(renewed.fencing_token,a.fencing_token)
        self.assertGreater(renewed.expires_at,a.expires_at)
        await eo.validate_execution_ownership(renewed)

    async def test_second_live_owner_is_rejected_while_first_lease_is_valid(self):
        await eo.acquire_execution_ownership("A")
        with self.assertRaises(eo.ExecutionOwnershipUnavailable):
            await eo.acquire_execution_ownership("B")

    async def test_heartbeat_db_failure_revokes_readiness_and_recovery_revalidates(self):
        ownership=await eo.acquire_execution_ownership("A")
        raw=SimpleNamespace(_execution_ownership=ownership)
        engine=SimpleNamespace(_running=True,_execution_ownership_valid=True,client=raw)
        calls=0
        async def sleep_once(_seconds):
            nonlocal calls
            calls += 1
            engine._running=False
        with patch.object(eo.db,"_conn",None), patch("bot.execution_ownership.asyncio.sleep",sleep_once):
            await eo.execution_ownership_heartbeat(engine)
        self.assertFalse(engine._execution_ownership_valid)
        engine._running=True
        async def stop_after_recovery(_seconds):
            engine._running=False
        with patch("bot.execution_ownership.asyncio.sleep",stop_after_recovery):
            await eo.execution_ownership_heartbeat(engine)
        self.assertTrue(engine._execution_ownership_valid)
        await eo.validate_execution_ownership(raw._execution_ownership)

    async def test_shadow_heartbeat_never_acquires_live_authority(self):
        engine=SimpleNamespace(_running=True,_execution_ownership_valid=True,client=SimpleNamespace())
        with patch.dict("os.environ",{"EXECUTION_CAPABILITY":"READ_ONLY"},clear=False), \
             patch("bot.execution_ownership.acquire_execution_ownership",AsyncMock()) as acquire:
            await eo.execution_ownership_heartbeat(engine)
        self.assertFalse(engine._execution_ownership_valid)
        acquire.assert_not_awaited()

    async def test_shadow_cannot_acquire_live_ownership(self):
        with patch.dict("os.environ",{"EXECUTION_CAPABILITY":"READ_ONLY"},clear=False):
            with self.assertRaises(eo.ExecutionOwnershipUnavailable):
                await eo.acquire_execution_ownership("shadow")

if __name__=="__main__": unittest.main()
