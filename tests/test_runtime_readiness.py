import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from bot.runtime_readiness import runtime_readiness, assert_ready_for_new_entries

def engine(**overrides):
    ownership=SimpleNamespace(expires_at=datetime.now(timezone.utc)+timedelta(seconds=30))
    d=dict(instruments={"BTC":{}},_durable_state_ok=True,_financial_state_sane=True,
      _initial_reconciliation_complete=True,_execution_ownership_valid=True,_execution_ownership_expires_at=ownership.expires_at,connected=True,
      _market_data_ready=True,_protection_system_ready=True,
      client=SimpleNamespace(_execution_ownership=ownership))
    d.update(overrides); return SimpleNamespace(**d)

class RuntimeReadinessTests(unittest.TestCase):
    def test_all_required_components_authorize(self):
      with patch.dict("os.environ",{"EXECUTION_CAPABILITY":"LIVE"},clear=False):
        self.assertTrue(runtime_readiness(engine()).ready_for_new_entries)
    def test_each_component_false_blocks_same_authority(self):
      fields=["instruments","_durable_state_ok","_financial_state_sane","_initial_reconciliation_complete",
       "_execution_ownership_valid","connected","_market_data_ready","_protection_system_ready"]
      with patch.dict("os.environ",{"EXECUTION_CAPABILITY":"LIVE"},clear=False):
       for field in fields:
        value={} if field=="instruments" else False
        with self.subTest(field=field):
          e=engine(**{field:value}); self.assertFalse(runtime_readiness(e).ready_for_new_entries)
          with self.assertRaises(RuntimeError): assert_ready_for_new_entries(e)
    def test_expired_local_lease_reverses_readiness_without_waiting_for_heartbeat(self):
      e=engine()
      self.assertTrue(runtime_readiness(e).ready_for_new_entries)
      e._execution_ownership_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)
      self.assertFalse(runtime_readiness(e).ready_for_new_entries)
      with self.assertRaises(RuntimeError): assert_ready_for_new_entries(e)
      e._execution_ownership_expires_at=datetime.now(timezone.utc)+timedelta(seconds=30)
      self.assertTrue(runtime_readiness(e).ready_for_new_entries)

    def test_read_only_blocks(self):
      with patch.dict("os.environ",{"EXECUTION_CAPABILITY":"READ_ONLY"},clear=False):
        self.assertFalse(runtime_readiness(engine()).ready_for_new_entries)

    def test_durable_state_cannot_substitute_for_explicit_reconciliation(self):
      e=engine()
      del e._initial_reconciliation_complete
      self.assertTrue(e._durable_state_ok)
      with patch.dict("os.environ",{"EXECUTION_CAPABILITY":"LIVE"},clear=False):
        snap=runtime_readiness(e)
      self.assertFalse(snap.initial_reconciliation_complete)
      self.assertFalse(snap.ready_for_new_entries)
class ProductionStartupOwnershipLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_startup_establishes_ownership_before_readiness(self):
      from bot.execution_ownership import initialize_live_execution_ownership
      e=engine(_execution_ownership_valid=False)
      e.client=SimpleNamespace()
      ownership=SimpleNamespace(owner_id="owner",fencing_token=7,expires_at=datetime.now(timezone.utc)+timedelta(seconds=30))
      with patch("bot.execution_ownership.acquire_execution_ownership",return_value=ownership) as acquire, \
           patch("bot.execution_ownership.validate_execution_ownership") as validate:
        await initialize_live_execution_ownership(e)
      acquire.assert_awaited_once()
      validate.assert_awaited_once_with(ownership)
      self.assertIs(e.client._execution_ownership,ownership)
      self.assertTrue(e._execution_ownership_valid)

if __name__=="__main__": unittest.main()
