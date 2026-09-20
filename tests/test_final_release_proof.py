import asyncio
import json
import math
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import execution_ownership as eo, kucoin
from bot.runtime_readiness import runtime_readiness
from tests.execution_test_context import ValidExecutionTestContext


class StopBeforeNetwork(RuntimeError):
    pass


class FinalReleaseProof(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = kucoin.KuCoinClient()
        self.client._instruments = {"BTCUSDT": {
            "tickSize": .1, "lotSize": 1, "qtyStep": 1, "multiplier": .001,
            "minQty": 1, "minBaseQty": .001, "minNotional": 0,
            "kucoinSymbol": "XBTUSDTM",
        }}

    async def test_execution_gate_order(self):
        calls = []
        engine = SimpleNamespace(
            instruments=self.client._instruments, _durable_state_ok=True,
            _financial_state_sane=True, _initial_reconciliation_complete=True,
            _execution_ownership_valid=False, connected=True, _market_data_ready=True,
            viable_symbols=["BTCUSDT"], _protection_system_ready=True,
        )
        self.client._engine = engine
        ownership = object()
        async def acquire():
            calls.append("ownership_acquire"); return ownership
        async def validate(_):
            calls.append("ownership")
        def critical():
            calls.append("critical_state")
        def ready(_):
            calls.append("readiness")
        async def exchange(*a, **k):
            calls.append("exchange_post"); return {"orderId": "one"}
        with patch.object(kucoin, "PAPER_TRADE", False), patch.object(kucoin, "API_KEY", "k"), \
             patch("bot.critical_state.critical_state.assert_available_for_new_risk", side_effect=critical), \
             patch("bot.execution_ownership.acquire_execution_ownership", side_effect=acquire), \
             patch("bot.execution_ownership.validate_execution_ownership", side_effect=validate), \
             patch("bot.runtime_readiness.assert_ready_for_new_entries", side_effect=ready), \
             patch.object(self.client, "_post", side_effect=exchange):
            await self.client.place_order("BTCUSDT", "Buy", .001, single_submission=True)
        projected=[x for x in calls if x in ("critical_state","ownership","readiness","exchange_post")]
        self.assertEqual(projected, ["critical_state","ownership","readiness","exchange_post"])

    async def test_each_entry_gate_failure_prevents_exchange_mutation(self):
        engine=SimpleNamespace()
        self.client._engine=engine
        cases=("critical","ownership","readiness")
        for failed in cases:
            post=AsyncMock()
            kwargs={}
            critical=AsyncMock() if False else None
            with self.subTest(failed=failed), \
                 patch.object(kucoin,"PAPER_TRADE",False), patch.object(kucoin,"API_KEY","k"), \
                 patch("bot.critical_state.critical_state.assert_available_for_new_risk",
                       side_effect=RuntimeError("critical") if failed=="critical" else None), \
                 patch("bot.execution_ownership.acquire_execution_ownership",
                       AsyncMock(return_value=object())), \
                 patch("bot.execution_ownership.validate_execution_ownership",
                       AsyncMock(side_effect=RuntimeError("ownership") if failed=="ownership" else None)), \
                 patch("bot.runtime_readiness.assert_ready_for_new_entries",
                       side_effect=RuntimeError("readiness") if failed=="readiness" else None), \
                 patch.object(self.client,"_post",post):
                self.client._execution_ownership=None
                with self.assertRaises(RuntimeError):
                    await self.client.place_order("BTCUSDT","Buy",.001)
                post.assert_not_awaited()

    async def test_transport_boundary_revalidates_fence_after_pipeline(self):
        self.client._execution_ownership=object()
        events=[]
        async def stale(_):
            events.append("final_fence")
            raise eo.StaleExecutionFence("REJECTED_STALE_FENCE")
        def would_post(*a,**k):
            events.append("exchange_post")
            raise StopBeforeNetwork()
        with patch.object(kucoin,"PAPER_TRADE",False), patch.object(self.client,"_ensure_session",AsyncMock()), \
             patch.object(self.client,"_throttle",AsyncMock()), \
             patch("bot.execution_ownership.validate_execution_ownership",side_effect=stale), \
             patch.object(self.client,"_entry_safe_post",side_effect=would_post):
            with self.assertRaises(eo.StaleExecutionFence):
                await self.client._post("/api/v1/orders",{"clientOid":"x","reduceOnly":False},single_attempt=True)
        self.assertEqual(events,["final_fence"])

    async def test_deploy_overlap_stale_owner_cannot_mutate(self):
        conn=ValidExecutionTestContext(self.client).conn
        with patch.object(eo.db,"_conn",conn), patch.object(eo.db,"_is_pg",True), \
             patch.object(eo.db,"configured_postgres_unavailable",return_value=False), \
             patch.dict("os.environ",{"EXECUTION_CAPABILITY":"LIVE"},clear=False):
            old=await eo.acquire_execution_ownership("old")
            state=json.loads(conn.value); state["expires_at"]="2000-01-01T00:00:00+00:00"; conn.value=json.dumps(state)
            new=await eo.acquire_execution_ownership("new")
            self.assertEqual(new.fencing_token,old.fencing_token+1)
            with self.assertRaises(eo.StaleExecutionFence):
                await eo.validate_execution_ownership(old)
            await eo.validate_execution_ownership(new)

    def test_financial_state_execution_matrix(self):
        base=dict(instruments={"BTC":{}},_durable_state_ok=True,_financial_state_sane=True,
            _initial_reconciliation_complete=True,_execution_ownership_valid=True,connected=True,
            _market_data_ready=True,_protection_system_ready=True)
        with patch.dict("os.environ",{"EXECUTION_CAPABILITY":"LIVE"},clear=False):
            self.assertTrue(runtime_readiness(SimpleNamespace(**base)).ready_for_new_entries)
            for label in ("nan","inf","negative_equity","negative_hwm","hwm_lt_equity",
                          "dd_negative","dd_gt_one","dd_mismatch","explosive_hwm"):
                e=SimpleNamespace(**{**base,"_financial_state_sane":False,
                    "_reconciliation_required":True,"financial_state_failure":label})
                with self.subTest(label=label):
                    snap=runtime_readiness(e)
                    self.assertFalse(snap.ready_for_new_entries)
                    self.assertTrue(e._reconciliation_required)

    async def test_fail_closed_does_not_block_reduce_only(self):
        with patch.object(kucoin,"PAPER_TRADE",False), patch.object(kucoin,"API_KEY","k"), \
             patch("bot.critical_state.critical_state.assert_available_for_new_risk",side_effect=RuntimeError("db")), \
             patch.object(self.client,"_post",AsyncMock(return_value={"orderId":"reduce"})) as post:
            out=await self.client.place_order("BTCUSDT","Sell",.001,reduce_only=True)
        self.assertEqual(out.get("orderId"),"reduce")
        post.assert_awaited_once()


if __name__=="__main__":
    unittest.main()
