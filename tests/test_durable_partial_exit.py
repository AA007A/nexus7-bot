from types import SimpleNamespace
from unittest.mock import AsyncMock
import unittest
from tests import test_confirmed_rr_exit as rr_tests
from bot.durable_partial_exit import check


class PartialTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        rr_tests.ConfirmedRRTests.setUp(self)
        self.position.qty_original = 1
        self.position.tp1_hit = False
        self.engine._unprotected_symbols = set()
        info = dict(multiplier=.01, lotSize=1, minQty=1, minNotional=0)
        self.engine.instruments = {'ETHUSDT': info}
        self.client._instruments = self.engine.instruments
        self.client.set_sl = AsyncMock(return_value=True)
        self.client.get_positions.return_value = [dict(symbol='ETHUSDT', size=.5, sizeUnit='BASE_ASSET')]

    async def test_partial_timeout_restart_never_resubmits(self):
        self.client.place_order.side_effect = TimeoutError
        await check(self.engine)
        self.client.get_order_by_client_oid.return_value = {'orderId': 'partial-1'}
        await check(SimpleNamespace(**vars(self.engine)))
        self.client.place_order.assert_awaited_once()
        self.assertFalse(self.position.tp1_hit)
        self.assertIn('ETHUSDT', self.engine._pending_partial_symbols)

    async def test_stop_exception_retries_protection_not_reduction(self):
        self.client.wait_for_fill.return_value = {'filled': True}
        self.client.set_sl.side_effect = [TimeoutError(), True]
        await check(self.engine)
        self.assertTrue(self.position.tp1_hit)
        self.assertEqual(self.position.qty, .5)
        self.assertEqual(self.position.sl, 99)
        await check(self.engine)
        self.client.place_order.assert_awaited_once()
        self.assertEqual(self.client.set_sl.await_count, 2)
        self.assertEqual(self.position.sl, 100)
        self.assertFalse(self.engine._pending_partial_symbols)

    async def test_restart_restores_filled_marker_without_second_order(self):
        self.client.wait_for_fill.return_value = {'filled': True}
        await check(self.engine)
        self.position.tp1_hit = False
        self.position.qty = 1
        await check(self.engine)
        self.assertTrue(self.position.tp1_hit)
        self.assertEqual(self.position.qty, .5)
        self.client.place_order.assert_awaited_once()

    async def test_partial_persistence_failure_and_invalid_units_fail_closed(self):
        self.client.wait_for_fill.return_value = {'filled': True}
        self.client.get_positions.return_value[0]['sizeUnit'] = 'UNKNOWN'
        await check(self.engine)
        self.client.set_sl.assert_not_awaited()
        self.assertEqual(self.position.qty, 1)

    async def test_nonfinite_exchange_size_cannot_be_treated_as_flat(self):
        self.client.wait_for_fill.return_value = {'filled': True}
        self.client.get_positions.return_value[0]['size'] = float('nan')
        await check(self.engine)
        self.engine._sync_positions.assert_not_awaited()
        self.client.set_sl.assert_not_awaited()

    async def test_partial_pending_blocks_rr_exit(self):
        await check(self.engine)
        await rr_tests.check(self.engine)
        self.client.place_order.assert_awaited_once()

    async def test_partial_intent_write_failure_prevents_network_submission(self):
        from unittest.mock import patch
        with patch('bot.confirmed_rr_exit.db.save_key_value', AsyncMock(return_value=False)):
            await check(self.engine)
        self.client.place_order.assert_not_awaited()

    async def test_pending_partial_blocks_new_entries(self):
        from bot.durable_execution import can_open
        self.engine._durable_state_ok = True
        await check(self.engine)
        self.assertFalse(can_open(self.engine))

    async def test_single_contract_cannot_be_partially_closed(self):
        self.position.qty_original = self.position.qty = .01
        await check(self.engine)
        self.client.place_order.assert_not_awaited()
