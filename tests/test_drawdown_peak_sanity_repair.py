import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import database as db
from bot import drawdown_persistence as dd
from bot import hwm_provenance
from bot import hwm_namespace


class DrawdownPeakSanityRepairTests(unittest.IsolatedAsyncioTestCase):
    def _assert_atomic(self, save, expected_peak):
        save.assert_awaited_once()
        items = list(save.await_args.args[0])
        self.assertEqual(items[0][0], dd.DURABLE_EQUITY_PEAK_KEY)
        self.assertAlmostEqual(float(items[0][1]), expected_peak, places=6)
        self.assertEqual(items[1][0], hwm_namespace.provenance_key())
        self.assertEqual(json.loads(items[1][1])["reason"], "incident_repair")

    async def test_known_20260914_corruption_repairs_to_last_good_peak(self):
        risk = SimpleNamespace(peak_balance=0.0, drawdown=0.0)
        with patch.object(db, "load_key_value", AsyncMock(return_value=str(dd._INCIDENT_BAD_PEAK))), \
             patch.object(dd, "save_key_values_atomic", AsyncMock(return_value=True)) as save:
            peak = await dd.restore_update_real_account_peak(risk, dd._INCIDENT_LAST_GOOD_PEAK, strict=True)
        self.assertAlmostEqual(peak, dd._INCIDENT_LAST_GOOD_PEAK, places=6)
        self.assertAlmostEqual(risk.drawdown, 0.0, places=9)
        self._assert_atomic(save, dd._INCIDENT_LAST_GOOD_PEAK)

    async def test_unrelated_implausible_peak_is_not_silently_rebased(self):
        risk = SimpleNamespace(peak_balance=0.0, drawdown=0.0)
        with patch.object(db, "load_key_value", AsyncMock(return_value="999999999")), \
             patch.object(dd, "save_key_values_atomic", AsyncMock(return_value=True)) as save:
            with self.assertRaises(db.PersistenceError):
                await dd.restore_update_real_account_peak(risk, 25.0, strict=True)
        save.assert_not_awaited()

    async def test_legitimate_historical_peak_remains_unchanged(self):
        risk = SimpleNamespace(peak_balance=0.0, drawdown=0.0)
        with patch.object(db, "load_key_value", AsyncMock(return_value="30.0")), \
             patch.object(dd, "save_key_values_atomic", AsyncMock(return_value=True)) as save:
            peak = await dd.restore_update_real_account_peak(risk, 28.0, strict=True)
        self.assertEqual(peak, 30.0)
        self.assertAlmostEqual(risk.drawdown, (30.0 - 28.0) / 30.0)
        save.assert_not_awaited()

    async def test_incident_signature_preserves_new_live_high(self):
        risk = SimpleNamespace(peak_balance=0.0, drawdown=0.0)
        with patch.object(db, "load_key_value", AsyncMock(return_value=str(dd._INCIDENT_BAD_PEAK))), \
             patch.object(dd, "save_key_values_atomic", AsyncMock(return_value=True)) as save:
            peak = await dd.restore_update_real_account_peak(risk, 100.0, strict=True)
        self.assertEqual(peak, 100.0)
        self.assertEqual(risk.drawdown, 0.0)
        self._assert_atomic(save, 100.0)


    async def test_transfer_in_near_zero_equity_rebases_additively(self):
        risk = SimpleNamespace(peak_balance=44.6737442789, drawdown=0.0)
        with patch.object(dd, "_load_peak", AsyncMock(return_value=(44.6737442789, "database"))), \
             patch.object(dd, "_write_peak_with_provenance", AsyncMock()) as write:
            peak = await dd.rebase_real_account_peak_for_external_flow(
                risk,
                19.1205130211,
                pre_flow_equity=2.11e-08,
                post_flow_equity=19.1205130211,
                flow_type="TransferIn",
                flow_amount=19.120513,
                flow_offset="91678290",
                strict=True,
            )
        self.assertAlmostEqual(peak, 63.7942572789, places=6)
        self.assertLess(peak, 100.0)
        write.assert_awaited_once()


    async def test_known_transfer_ratio_corruption_repairs_to_additive_peak(self):
        risk = SimpleNamespace(peak_balance=0.0, drawdown=0.0)
        with patch.object(
            db, "load_key_value",
            AsyncMock(return_value=str(dd._TRANSFER_RATIO_INCIDENT_BAD_PEAK))
        ), patch.object(
            dd, "save_key_values_atomic", AsyncMock(return_value=True)
        ) as save:
            peak = await dd.restore_update_real_account_peak(risk, 19.1205130211, strict=True)
        self.assertAlmostEqual(peak, dd._TRANSFER_RATIO_INCIDENT_REPAIRED_PEAK, places=6)
        self.assertLess(peak, 100.0)
        self._assert_atomic(save, dd._TRANSFER_RATIO_INCIDENT_REPAIRED_PEAK)


if __name__ == "__main__": unittest.main()
