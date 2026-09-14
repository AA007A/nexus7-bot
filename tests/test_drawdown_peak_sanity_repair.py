import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import database as db
from bot import drawdown_persistence as dd


class DrawdownPeakSanityRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_20260914_corruption_repairs_to_last_good_peak(self):
        risk = SimpleNamespace(peak_balance=0.0, drawdown=0.0)
        bad = str(dd._INCIDENT_BAD_PEAK)

        with patch.object(db, "load_key_value", AsyncMock(return_value=bad)), patch.object(
            db, "save_key_value", AsyncMock(return_value=True)
        ) as save:
            peak = await dd.restore_update_real_account_peak(
                risk, dd._INCIDENT_LAST_GOOD_PEAK, strict=True
            )

        self.assertAlmostEqual(peak, dd._INCIDENT_LAST_GOOD_PEAK, places=6)
        self.assertAlmostEqual(risk.peak_balance, dd._INCIDENT_LAST_GOOD_PEAK, places=6)
        self.assertAlmostEqual(risk.drawdown, 0.0, places=9)
        save.assert_awaited()
        saved_args = save.await_args.args
        self.assertEqual(saved_args[0], dd.DURABLE_EQUITY_PEAK_KEY)
        self.assertAlmostEqual(float(saved_args[1]), dd._INCIDENT_LAST_GOOD_PEAK, places=6)

    async def test_unrelated_implausible_peak_is_not_silently_rebased(self):
        risk = SimpleNamespace(peak_balance=0.0, drawdown=0.0)
        with patch.object(db, "load_key_value", AsyncMock(return_value="999999999")), patch.object(
            db, "save_key_value", AsyncMock(return_value=True)
        ) as save:
            with self.assertRaises(db.PersistenceError):
                await dd.restore_update_real_account_peak(risk, 25.0, strict=True)
        save.assert_not_awaited()

    async def test_legitimate_historical_peak_remains_unchanged(self):
        risk = SimpleNamespace(peak_balance=0.0, drawdown=0.0)
        with patch.object(db, "load_key_value", AsyncMock(return_value="30.0")), patch.object(
            db, "save_key_value", AsyncMock(return_value=True)
        ) as save:
            peak = await dd.restore_update_real_account_peak(risk, 28.0, strict=True)
        self.assertEqual(peak, 30.0)
        self.assertAlmostEqual(risk.drawdown, (30.0 - 28.0) / 30.0)
        save.assert_not_awaited()

    async def test_incident_signature_requires_live_equity_match(self):
        risk = SimpleNamespace(peak_balance=0.0, drawdown=0.0)
        with patch.object(
            db, "load_key_value", AsyncMock(return_value=str(dd._INCIDENT_BAD_PEAK))
        ), patch.object(db, "save_key_value", AsyncMock(return_value=True)) as save:
            with self.assertRaises(db.PersistenceError):
                await dd.restore_update_real_account_peak(risk, 100.0, strict=True)
        save.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
