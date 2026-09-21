import json, unittest
import asyncio
from types import SimpleNamespace
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from bot import execution_ownership as eo
from bot.runtime_readiness import runtime_readiness

def ready_engine(client=None):
    return SimpleNamespace(
        _running=True,
        instruments={"BTCUSDT":{}},
        _durable_state_ok=True,
        _financial_state_sane=True,
        _initial_reconciliation_complete=True,
        _execution_ownership_valid=False,
        _execution_ownership_expires_at=None,
        connected=True,
        _market_data_ready=True,
        viable_symbols=["BTCUSDT"],
        _protection_system_ready=True,
        client=client or SimpleNamespace(),
    )

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


    async def test_heartbeat_multiple_cycles_renews_same_fence(self):
        initial=await eo.acquire_execution_ownership()
        engine=ready_engine(SimpleNamespace(_execution_ownership=initial))
        observed=[]
        snapshots=[]
        real_acquire=eo.acquire_execution_ownership
        async def recording_acquire(*args,**kwargs):
            item=await real_acquire(*args,**kwargs)
            observed.append(item)
            return item
        cycles=0
        async def bounded_sleep(_seconds):
            nonlocal cycles
            snapshots.append(runtime_readiness(engine))
            cycles += 1
            if cycles >= 3:
                engine._running=False
        with patch("bot.execution_ownership.acquire_execution_ownership",side_effect=recording_acquire), \
             patch("asyncio.sleep",bounded_sleep):
            await eo.execution_ownership_heartbeat(engine)
        self.assertEqual(len(observed),3)
        self.assertEqual({x.owner_id for x in observed},{initial.owner_id})
        self.assertEqual({x.session_id for x in observed},{initial.session_id})
        self.assertEqual({x.fencing_token for x in observed},{initial.fencing_token})
        self.assertTrue(all(b.expires_at >= a.expires_at for a,b in zip(observed,observed[1:])))
        self.assertTrue(engine._execution_ownership_valid)
        self.assertTrue(all(s.execution_ownership_valid for s in snapshots))
        self.assertTrue(all(s.ready_for_new_entries for s in snapshots))


    async def test_valid_real_ownership_is_visible_to_runtime_readiness(self):
        engine=ready_engine()
        ownership=await eo.initialize_live_execution_ownership(engine)
        await eo.validate_execution_ownership(ownership)
        self.assertIsInstance(ownership.expires_at,str)
        self.assertTrue(engine._execution_ownership_valid)
        self.assertTrue(
            runtime_readiness(engine).execution_ownership_valid,
            "validated DB ownership must be visible to readiness",
        )

    async def test_second_live_owner_is_rejected_while_first_lease_is_valid(self):
        await eo.acquire_execution_ownership("A")
        with self.assertRaises(eo.ExecutionOwnershipUnavailable):
            await eo.acquire_execution_ownership("B")

    async def test_heartbeat_db_failure_revokes_readiness_and_recovery_revalidates(self):
        ownership=await eo.acquire_execution_ownership()
        raw=SimpleNamespace(_execution_ownership=ownership)
        engine=ready_engine(raw)
        valid_until=await eo.validate_execution_ownership(ownership)
        eo.apply_validated_execution_ownership(
            engine,ownership,valid_until,event="test_setup",
        )
        self.assertTrue(runtime_readiness(engine).ready_for_new_entries)
        async def sleep_once(_seconds):
            engine._running=False
        with patch.object(eo.db,"_conn",None), patch("asyncio.sleep",sleep_once):
            await eo.execution_ownership_heartbeat(engine)
        self.assertFalse(engine._execution_ownership_valid)
        self.assertFalse(runtime_readiness(engine).ready_for_new_entries)
        engine._running=True
        async def stop_after_recovery(_seconds):
            engine._running=False
        with patch("asyncio.sleep",stop_after_recovery):
            await eo.execution_ownership_heartbeat(engine)
        self.assertTrue(engine._execution_ownership_valid)
        self.assertTrue(runtime_readiness(engine).ready_for_new_entries)
        await eo.validate_execution_ownership(raw._execution_ownership)


    async def test_local_expiry_then_valid_reacquire_restores_readiness(self):
        engine=ready_engine()
        ownership=await eo.initialize_live_execution_ownership(engine)
        self.assertTrue(runtime_readiness(engine).ready_for_new_entries)
        engine._execution_ownership_expires_at=eo._now()-eo.timedelta(seconds=1)
        self.assertFalse(runtime_readiness(engine).ready_for_new_entries)
        renewed=await eo.acquire_execution_ownership()
        valid_until=await eo.validate_execution_ownership(renewed)
        eo.apply_validated_execution_ownership(
            engine,renewed,valid_until,event="heartbeat_renewed",
        )
        self.assertTrue(runtime_readiness(engine).ready_for_new_entries)
        self.assertEqual(renewed.fencing_token,ownership.fencing_token)

    async def test_takeover_stale_owner_invalidates_while_new_owner_is_valid(self):
        a=await eo.acquire_execution_ownership("A")
        state=json.loads(self.conn.value)
        state["expires_at"]="2000-01-01T00:00:00+00:00"
        self.conn.value=json.dumps(state)
        b=await eo.acquire_execution_ownership("B")
        engine_a=ready_engine(SimpleNamespace(_execution_ownership=a))
        engine_b=ready_engine(SimpleNamespace(_execution_ownership=b))
        with self.assertRaises(eo.StaleExecutionFence):
            await eo.validate_execution_ownership(a)
        eo.invalidate_local_execution_ownership(
            engine_a,event="takeover_detected",reason="StaleExecutionFence",
        )
        valid_until_b=await eo.validate_execution_ownership(b)
        eo.apply_validated_execution_ownership(
            engine_b,b,valid_until_b,event="startup_validated",
        )
        self.assertFalse(runtime_readiness(engine_a).execution_ownership_valid)
        self.assertTrue(runtime_readiness(engine_b).execution_ownership_valid)
        self.assertEqual(b.fencing_token,a.fencing_token+1)

    async def test_runtime_readiness_read_does_not_mutate_ownership_state(self):
        engine=ready_engine()
        await eo.initialize_live_execution_ownership(engine)
        before=(
            engine._execution_ownership_valid,
            engine._execution_ownership_expires_at,
            engine.client._execution_ownership,
        )
        snap=runtime_readiness(engine)
        after=(
            engine._execution_ownership_valid,
            engine._execution_ownership_expires_at,
            engine.client._execution_ownership,
        )
        self.assertTrue(snap.execution_ownership_valid)
        self.assertEqual(before,after)

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
