import unittest
from unittest.mock import AsyncMock, patch

from bot import database as db
from bot.drawdown_persistence import restore_update_real_account_peak
from bot.risk import RiskManager


class SecondObservedHwmIncidentTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_observed_corruption_remains_fail_closed_until_evidence_bounded_repair(self):
        risk = RiskManager()
        risk.init(38.1680518389)
        with patch(
            "bot.drawdown_persistence.db.load_key_value",
            AsyncMock(return_value="38935582822.336571"),
        ), patch(
            "bot.drawdown_persistence.save_key_values_atomic",
            AsyncMock(return_value=True),
        ) as save:
            with self.assertRaises(db.PersistenceError):
                await restore_update_real_account_peak(risk, 38.1680518389, strict=True)
        save.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
