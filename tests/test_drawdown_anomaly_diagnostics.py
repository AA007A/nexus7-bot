import unittest
from unittest.mock import AsyncMock, patch

from bot import database as db
from bot.drawdown_persistence import restore_update_real_account_peak
from bot.risk import RiskManager


class DurableDrawdownAnomalyDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_implausible_hwm_logs_evidence_and_still_fails_closed(self):
        risk = RiskManager(14.2)
        with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value="90000000000")), \
             patch("bot.drawdown_persistence.save_key_values_atomic", AsyncMock(return_value=True)) as save, \
             patch("bot.drawdown_persistence.log.critical") as critical:
            with self.assertRaises(db.PersistenceError):
                await restore_update_real_account_peak(risk, 14.2, strict=True)

        save.assert_not_awaited()
        critical.assert_called_once()
        args = critical.call_args.args
        self.assertIn("[DURABLE_DRAWDOWN_ANOMALY]", args[0])
        self.assertEqual(args[1], 90_000_000_000.0)
        self.assertEqual(args[2], 14.2)
        self.assertGreater(args[3], 1_000.0)
        self.assertFalse(args[5])


if __name__ == "__main__":
    unittest.main()
