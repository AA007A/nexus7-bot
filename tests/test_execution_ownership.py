import json, unittest
import asyncio
from types import SimpleNamespace
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from bot import execution_ownership as eo
from bot.runtime_readiness import runtime_readiness

ZERO_PROTECTION_RECEIPT = {
    "observed_symbols": [],
    "positions": 0,
    "protected_positions": 0,
    "unprotected_positions": 0,
    "readback_complete": True,
    "evidence": {},
}


def _protection_ready_fields():
    return {
        "_protection_system_ready": True,
        "_protection_readiness_receipt": dict(ZERO_PROTECTION_RECEIPT),
        "positions": {},
        "_external_position_symbols": set(),
        "_unprotected_symbols": set(),
    }

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
        engine=SimpleNamespace(
            _running=True,
            _execution_ownership_valid=True,
            _execution_ownership_expires_at=eo._parse(initial.expires_at),
            client=SimpleNamespace(_execution_ownership=initial),
            instruments={"BTCUSDT": {}},
            _durable_state_ok=True,
            _financial_state_sane=True,
            _initial_reconciliation_complete=True,
            connected=True,
            viable_symbols=["BTCUSDT"],
            _market_data_ready=True,
            **_protection_ready_fields(),
        )
        observed=[]
        readiness=[]
        real_acquire=eo.acquire_execution_ownership
        async def recording_acquire(*args,**kwargs):
            item=await real_acquire(*args,**kwargs)
            observed.append(item)
            return item
        cycles=0
        async def bounded_sleep(_seconds):
            nonlocal cycles
            readiness.append(runtime_readiness(engine).ready_for_new_entries)
            cycles += 1
            if cycles >= 3:
                engine._running=False
        with patch("bot.execution_ownership.acquire_execution_ownership",side_effect=recording_acquire), \
             patch("asyncio.sleep",bounded_sleep):
            await eo.execution_ownership_heartbeat(engine)
        self.assertEqual(len(observed),3)
        self.assertEqual(readiness,[True,True,True])
        self.assertEqual({x.owner_id for x in observed},{initial.owner_id})
        self.assertEqual({x.session_id for x in observed},{initial.session_id})
        self.assertEqual({x.fencing_token for x in observed},{initial.fencing_token})
        self.assertTrue(all(b.expires_at >= a.expires_at for a,b in zip(observed,observed[1:])))
        self.assertTrue(engine._execution_ownership_valid)
        self.assertTrue(runtime_readiness(engine).execution_ownership_valid)
        self.assertIsInstance(engine._execution_ownership_expires_at, __import__("datetime").datetime)

    async def test_valid_heartbeat_lease_is_visible_to_runtime_readiness(self):
        initial=await eo.acquire_execution_ownership()
        engine=SimpleNamespace(
            _running=True,
            _execution_ownership_valid=True,
            _execution_ownership_expires_at=initial.expires_at,
            client=SimpleNamespace(_execution_ownership=initial),
            instruments={"BTCUSDT": {}},
            _durable_state_ok=True,
            _financial_state_sane=True,
            _initial_reconciliation_complete=True,
            connected=True,
            viable_symbols=["BTCUSDT"],
            _market_data_ready=True,
            **_protection_ready_fields(),
        )
        async def stop_after_one(_seconds):
            engine._running=False
        with patch("asyncio.sleep",stop_after_one):
            await eo.execution_ownership_heartbeat(engine)
        self.assertTrue(engine._execution_ownership_valid)
        self.assertTrue(runtime_readiness(engine).execution_ownership_valid)

    async def test_local_expiry_then_valid_renewal_restores_readiness(self):
        ownership=await eo.acquire_execution_ownership()
        engine=SimpleNamespace(
            _running=False,
            _execution_ownership_valid=True,
            _execution_ownership_expires_at=__import__("datetime").datetime(2000,1,1,tzinfo=__import__("datetime").timezone.utc),
            client=SimpleNamespace(_execution_ownership=ownership),
            instruments={"BTCUSDT": {}}, _durable_state_ok=True,
            _financial_state_sane=True, _initial_reconciliation_complete=True,
            connected=True, viable_symbols=["BTCUSDT"], _market_data_ready=True,
            **_protection_ready_fields(),
        )
        self.assertFalse(runtime_readiness(engine).execution_ownership_valid)
        eo.publish_valid_execution_ownership(engine, ownership, event="startup_validated")
        self.assertTrue(runtime_readiness(engine).execution_ownership_valid)

    async def test_runtime_readiness_read_does_not_mutate_ownership(self):
        ownership=await eo.acquire_execution_ownership()
        engine=SimpleNamespace(
            _execution_ownership_valid=True,
            _execution_ownership_expires_at=eo._parse(ownership.expires_at),
        )
        before=(engine._execution_ownership_valid,engine._execution_ownership_expires_at)
        runtime_readiness(engine)
        after=(engine._execution_ownership_valid,engine._execution_ownership_expires_at)
        self.assertEqual(before,after)

    async def test_takeover_invalidates_stale_process_and_new_owner_stays_ready(self):
        a = await eo.acquire_execution_ownership("A")
        engine_a = SimpleNamespace(
            _running=True,
            _execution_ownership_valid=True,
            _execution_ownership_expires_at=eo._parse(a.expires_at),
            client=SimpleNamespace(_execution_ownership=a),
            instruments={"BTCUSDT": {}}, _durable_state_ok=True,
            _financial_state_sane=True, _initial_reconciliation_complete=True,
            connected=True, viable_symbols=["BTCUSDT"], _market_data_ready=True,
            **_protection_ready_fields(),
        )
        state=json.loads(self.conn.value)
        state["expires_at"]="2000-01-01T00:00:00+00:00"
        self.conn.value=json.dumps(state)
        b = await eo.acquire_execution_ownership("B")
        engine_b = SimpleNamespace(
            _execution_ownership_valid=False,
            _execution_ownership_expires_at=None,
            client=SimpleNamespace(_execution_ownership=b),
            instruments={"BTCUSDT": {}}, _durable_state_ok=True,
            _financial_state_sane=True, _initial_reconciliation_complete=True,
            connected=True, viable_symbols=["BTCUSDT"], _market_data_ready=True,
            **_protection_ready_fields(),
        )
        await eo.validate_execution_ownership(b)
        eo.publish_valid_execution_ownership(engine_b,b,event="startup_validated")
        self.assertTrue(runtime_readiness(engine_b).ready_for_new_entries)

        async def stop_after_failure(_seconds):
            engine_a._running=False
        with patch("asyncio.sleep",stop_after_failure):
            await eo.execution_ownership_heartbeat(engine_a)
        self.assertFalse(engine_a._execution_ownership_valid)
        self.assertFalse(runtime_readiness(engine_a).ready_for_new_entries)
        self.assertTrue(engine_b._execution_ownership_valid)
        self.assertTrue(runtime_readiness(engine_b).ready_for_new_entries)

    async def test_second_live_owner_is_rejected_while_first_lease_is_valid(self):
        await eo.acquire_execution_ownership("A")
        with self.assertRaises(eo.ExecutionOwnershipUnavailable):
            await eo.acquire_execution_ownership("B")

    async def test_heartbeat_db_failure_revokes_readiness_and_recovery_revalidates(self):
        ownership=await eo.acquire_execution_ownership()
        raw=SimpleNamespace(_execution_ownership=ownership)
        engine=SimpleNamespace(
            _running=True,
            _execution_ownership_valid=True,
            _execution_ownership_expires_at=eo._parse(ownership.expires_at),
            client=raw,
            instruments={"BTCUSDT": {}},
            _durable_state_ok=True,
            _financial_state_sane=True,
            _initial_reconciliation_complete=True,
            connected=True,
            viable_symbols=["BTCUSDT"],
            _market_data_ready=True,
            **_protection_ready_fields(),
        )
        calls=0
        async def sleep_once(_seconds):
            nonlocal calls
            calls += 1
            engine._running=False
        with patch.object(eo.db,"_conn",None), patch("asyncio.sleep",sleep_once):
            await eo.execution_ownership_heartbeat(engine)
        self.assertFalse(engine._execution_ownership_valid)
        self.assertFalse(runtime_readiness(engine).execution_ownership_valid)
        engine._running=True
        async def stop_after_recovery(_seconds):
            engine._running=False
        with patch("asyncio.sleep",stop_after_recovery):
            await eo.execution_ownership_heartbeat(engine)
        self.assertTrue(engine._execution_ownership_valid)
        self.assertTrue(runtime_readiness(engine).execution_ownership_valid)
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
