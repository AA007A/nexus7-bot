import unittest
from unittest.mock import AsyncMock, patch

from bot import database as db
from bot import hwm_provenance
from bot.drawdown_persistence import (
    DURABLE_EQUITY_PEAK_KEY,
    restore_update_real_account_peak,
)
from bot.professional_risk import CapitalState
from bot.professional_risk_adapter import ProfessionalRiskAdapter
from bot.risk import RiskManager


INCIDENT_BAD_PEAK = 82_894_351_780.2826
LAST_GOOD_PEAK = 28.7914


class DurableDrawdownIncidentRepairTests(unittest.IsolatedAsyncioTestCase):
    def _risk(self, equity: float) -> tuple[RiskManager, ProfessionalRiskAdapter]:
        legacy = RiskManager()
        legacy.init(equity)
        risk = ProfessionalRiskAdapter(legacy)
        risk.update_capital(CapitalState(equity, equity))
        return legacy, risk

    async def test_exact_incident_signature_repairs_to_newer_live_equity_high(self):
        legacy, risk = self._risk(32.0573)
        with patch(
            "bot.drawdown_persistence.db.load_key_value",
            AsyncMock(return_value=str(INCIDENT_BAD_PEAK)),
        ), patch(
            "bot.drawdown_persistence.db.save_key_value",
            AsyncMock(return_value=True),
        ) as save:
            peak = await restore_update_real_account_peak(risk, 32.0573, strict=True)

        self.assertAlmostEqual(peak, 32.0573, places=6)
        self.assertAlmostEqual(legacy.peak_balance, 32.0573, places=6)
        self.assertAlmostEqual(legacy.drawdown, 0.0, places=9)
        self.assertEqual(save.await_count, 2)
        self.assertEqual(save.await_args_list[0].args[0], DURABLE_EQUITY_PEAK_KEY)
        self.assertAlmostEqual(float(save.await_args_list[0].args[1]), 32.0573, places=6)
        self.assertEqual(save.await_args_list[1].args[0], hwm_provenance.HWM_PROVENANCE_KEY)

    async def test_exact_incident_signature_never_repairs_below_last_good_peak(self):
        legacy, risk = self._risk(23.0)
        with patch(
            "bot.drawdown_persistence.db.load_key_value",
            AsyncMock(return_value=str(INCIDENT_BAD_PEAK)),
        ), patch(
            "bot.drawdown_persistence.db.save_key_value",
            AsyncMock(return_value=True),
        ):
            peak = await restore_update_real_account_peak(risk, 23.0, strict=True)

        self.assertAlmostEqual(peak, LAST_GOOD_PEAK, places=6)
        self.assertAlmostEqual(legacy.peak_balance, LAST_GOOD_PEAK, places=6)
        self.assertAlmostEqual(
            legacy.drawdown,
            (LAST_GOOD_PEAK - 23.0) / LAST_GOOD_PEAK,
            places=9,
        )

    async def test_unrelated_implausible_peak_still_fails_closed(self):
        _, risk = self._risk(32.0573)
        with patch(
            "bot.drawdown_persistence.db.load_key_value",
            AsyncMock(return_value="90000000000"),
        ), patch(
            "bot.drawdown_persistence.db.save_key_value",
            AsyncMock(return_value=True),
        ) as save:
            with self.assertRaises(db.PersistenceError):
                await restore_update_real_account_peak(risk, 32.0573, strict=True)

        save.assert_not_awaited()

    async def test_signature_outside_tolerance_is_not_repaired(self):
        _, risk = self._risk(32.0573)
        with patch(
            "bot.drawdown_persistence.db.load_key_value",
            AsyncMock(return_value=str(INCIDENT_BAD_PEAK + 2.0)),
        ), patch(
            "bot.drawdown_persistence.db.save_key_value",
            AsyncMock(return_value=True),
        ) as save:
            with self.assertRaises(db.PersistenceError):
                await restore_update_real_account_peak(risk, 32.0573, strict=True)

        save.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
