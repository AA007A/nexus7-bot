import json
import unittest
from unittest.mock import AsyncMock, patch

from bot import database as db
from bot import hwm_provenance
from bot.drawdown_persistence import DURABLE_EQUITY_PEAK_KEY, restore_update_real_account_peak
from bot.professional_risk import CapitalState
from bot.professional_risk_adapter import ProfessionalRiskAdapter
from bot.risk import RiskManager

INCIDENT_BAD_PEAK = 82_894_351_780.2826
INCIDENT_LAST_GOOD_PEAK = 28.7914


class DurableDrawdownIncidentRepairTests(unittest.IsolatedAsyncioTestCase):
    def _risk(self, equity):
        legacy = RiskManager(); legacy.init(equity)
        risk = ProfessionalRiskAdapter(legacy); risk.update_capital(CapitalState(equity, equity))
        return legacy, risk

    def _assert_atomic_repair(self, save, expected_peak):
        save.assert_awaited_once()
        items = list(save.await_args.args[0])
        self.assertEqual(items[0][0], DURABLE_EQUITY_PEAK_KEY)
        self.assertAlmostEqual(float(items[0][1]), expected_peak, places=6)
        self.assertEqual(items[1][0], hwm_provenance.HWM_PROVENANCE_KEY)
        self.assertEqual(json.loads(items[1][1])["reason"], "incident_repair")

    async def test_exact_incident_signature_repairs_to_last_good_peak(self):
        legacy, risk = self._risk(23.0)
        with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value=str(INCIDENT_BAD_PEAK))), \
             patch("bot.drawdown_persistence.save_key_values_atomic", AsyncMock(return_value=True)) as save:
            peak = await restore_update_real_account_peak(risk, 23.0, strict=True)
        self.assertAlmostEqual(peak, INCIDENT_LAST_GOOD_PEAK, places=6)
        self.assertAlmostEqual(
            legacy.drawdown,
            (INCIDENT_LAST_GOOD_PEAK - 23.0) / INCIDENT_LAST_GOOD_PEAK,
            places=9,
        )
        self._assert_atomic_repair(save, INCIDENT_LAST_GOOD_PEAK)

    async def test_exact_incident_signature_never_repairs_below_current_equity(self):
        legacy, risk = self._risk(32.0573)
        with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value=str(INCIDENT_BAD_PEAK))), \
             patch("bot.drawdown_persistence.save_key_values_atomic", AsyncMock(return_value=True)) as save:
            peak = await restore_update_real_account_peak(risk, 32.0573, strict=True)
        self.assertAlmostEqual(peak, 32.0573, places=6)
        self.assertAlmostEqual(legacy.drawdown, 0.0, places=9)
        self._assert_atomic_repair(save, 32.0573)

    async def test_unproven_legacy_variant_fails_closed_even_for_small_account(self):
        _, risk = self._risk(14.2)
        with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value="90000000000")), \
             patch("bot.drawdown_persistence.save_key_values_atomic", AsyncMock(return_value=True)) as save:
            with self.assertRaises(db.PersistenceError):
                await restore_update_real_account_peak(risk, 14.2, strict=True)
        save.assert_not_awaited()

    async def test_unrelated_large_account_implausible_peak_still_fails_closed(self):
        _, risk = self._risk(200.0)
        with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value="90000000000")), \
             patch("bot.drawdown_persistence.save_key_values_atomic", AsyncMock(return_value=True)) as save:
            with self.assertRaises(db.PersistenceError):
                await restore_update_real_account_peak(risk, 200.0, strict=True)
        save.assert_not_awaited()

    async def test_high_but_non_incident_peak_is_not_silently_lowered(self):
        _, risk = self._risk(14.2)
        with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value="10000")), \
             patch("bot.drawdown_persistence.save_key_values_atomic", AsyncMock(return_value=True)) as save:
            peak = await restore_update_real_account_peak(risk, 14.2, strict=True)
        self.assertAlmostEqual(peak, 10000.0, places=6)
        save.assert_not_awaited()


if __name__ == "__main__": unittest.main()
