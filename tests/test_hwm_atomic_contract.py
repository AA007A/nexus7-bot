import unittest
from unittest.mock import AsyncMock, patch

from bot import database as db
from bot.drawdown_persistence import restore_update_real_account_peak
from bot.professional_risk import CapitalState
from bot.professional_risk_adapter import ProfessionalRiskAdapter
from bot.risk import RiskManager


class HwmAtomicContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_atomic_transition_does_not_advance_runtime_peak(self):
        legacy = RiskManager(); legacy.init(100.0)
        risk = ProfessionalRiskAdapter(legacy); risk.update_capital(CapitalState(100.0, 80.0))
        with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value="100")), \
             patch("bot.drawdown_persistence.save_key_values_atomic", AsyncMock(side_effect=db.PersistenceError("transaction rolled back"))):
            with self.assertRaises(db.PersistenceError):
                await restore_update_real_account_peak(risk, 125.0, strict=True)
        self.assertEqual(legacy.peak_balance, 100.0)
        self.assertIsNone(getattr(risk, "_durable_account_equity_peak", None))


if __name__ == "__main__": unittest.main()
