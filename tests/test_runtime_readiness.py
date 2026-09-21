import unittest
from types import SimpleNamespace
from unittest.mock import patch
from bot.runtime_readiness import runtime_readiness, assert_ready_for_new_entries

def engine(**overrides):
    d=dict(instruments={"BTC":{}},_durable_state_ok=True,_financial_state_sane=True,
      _initial_reconciliation_complete=True,_execution_ownership_valid=True,connected=True,
      _market_data_ready=True,_protection_system_ready=True)
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
    def test_read_only_blocks(self):
      with patch.dict("os.environ",{"EXECUTION_CAPABILITY":"READ_ONLY"},clear=False):
        self.assertFalse(runtime_readiness(engine()).ready_for_new_entries)
if __name__=="__main__": unittest.main()
