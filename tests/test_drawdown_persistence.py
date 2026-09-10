import math
import unittest
from unittest.mock import AsyncMock, patch

from bot import database as db
from bot.drawdown_persistence import (
    DURABLE_EQUITY_PEAK_KEY,
    rebase_real_account_peak_for_external_flow,
    restore_update_real_account_peak,
)
from bot.professional_risk import CapitalState
from bot.professional_risk_adapter import ProfessionalRiskAdapter
from bot.risk import RiskManager
from bot.risk_manager_v3 import RiskManagerV3


class DurableDrawdownPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.legacy = RiskManager()
        self.legacy.init(100.0)
        self.risk = ProfessionalRiskAdapter(self.legacy)
        self.risk.update_capital(CapitalState(100.0, 80.0))

    async def test_missing_state_bootstraps_verified_equity(self):
        with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value=None)) as load, \
             patch("bot.drawdown_persistence.db.save_key_value", AsyncMock(return_value=True)) as save:
            peak = await restore_update_real_account_peak(self.risk, 100.0, strict=True)

        self.assertEqual(peak, 100.0)
        self.assertEqual(self.legacy.peak_balance, 100.0)
        self.assertEqual(self.legacy.drawdown, 0.0)
        load.assert_awaited_once_with(DURABLE_EQUITY_PEAK_KEY, strict=True)
        save.assert_awaited_once()

    async def test_restart_restores_higher_peak_and_nonzero_drawdown(self):
        fresh_legacy = RiskManager()
        fresh_legacy.init(80.0)
        fresh = ProfessionalRiskAdapter(fresh_legacy)
        fresh.update_capital(CapitalState(80.0, 60.0))

        with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value="100")), \
             patch("bot.drawdown_persistence.db.save_key_value", AsyncMock(return_value=True)) as save:
            peak = await restore_update_real_account_peak(fresh, 80.0, strict=True)

        self.assertEqual(peak, 100.0)
        self.assertAlmostEqual(fresh_legacy.drawdown, 0.20)
        self.assertAlmostEqual(fresh.professional_snapshot.drawdown, 0.20)
        save.assert_not_awaited()

    async def test_new_high_raises_and_persists_peak(self):
        with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value="100")), \
             patch("bot.drawdown_persistence.db.save_key_value", AsyncMock(return_value=True)) as save:
            peak = await restore_update_real_account_peak(self.risk, 125.0, strict=True)

        self.assertEqual(peak, 125.0)
        self.assertEqual(self.legacy.peak_balance, 125.0)
        save.assert_awaited_once()

    async def test_cached_peak_never_decreases_and_avoids_reloading(self):
        with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value="120")) as load, \
             patch("bot.drawdown_persistence.db.save_key_value", AsyncMock(return_value=True)):
            await restore_update_real_account_peak(self.risk, 100.0, strict=True)
            await restore_update_real_account_peak(self.risk, 90.0, strict=True)

        self.assertEqual(self.legacy.peak_balance, 120.0)
        self.assertAlmostEqual(self.legacy.drawdown, 0.25)
        load.assert_awaited_once()

    async def test_verified_withdrawal_preserves_pre_flow_drawdown(self):
        legacy = RiskManager()
        legacy.init(20.8664)
        risk = ProfessionalRiskAdapter(legacy)
        risk.update_capital(CapitalState(20.8664, 20.8664))

        with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value="38.2593")), \
             patch("bot.drawdown_persistence.db.save_key_value", AsyncMock(return_value=True)) as save:
            peak = await rebase_real_account_peak_for_external_flow(
                risk,
                20.8664,
                pre_flow_equity=34.8664,
                post_flow_equity=20.8664,
                flow_type="TransferOut",
                flow_amount=14.0,
                flow_offset="12345",
                strict=True,
            )

        expected_peak = 38.2593 * (20.8664 / 34.8664)
        expected_dd = (expected_peak - 20.8664) / expected_peak
        pre_flow_dd = (38.2593 - 34.8664) / 38.2593
        self.assertAlmostEqual(peak, expected_peak, places=6)
        self.assertAlmostEqual(legacy.drawdown, expected_dd, places=6)
        self.assertAlmostEqual(expected_dd, pre_flow_dd, places=6)
        self.assertLess(legacy.drawdown, 0.10)
        self.assertAlmostEqual(risk.professional_snapshot.drawdown, expected_dd, places=6)
        save.assert_awaited_once()

    async def test_verified_deposit_preserves_pre_flow_drawdown(self):
        legacy = RiskManager()
        legacy.init(120.0)
        risk = ProfessionalRiskAdapter(legacy)
        risk.update_capital(CapitalState(120.0, 120.0))

        with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value="100")), \
             patch("bot.drawdown_persistence.db.save_key_value", AsyncMock(return_value=True)):
            peak = await rebase_real_account_peak_for_external_flow(
                risk,
                120.0,
                pre_flow_equity=80.0,
                post_flow_equity=120.0,
                flow_type="TransferIn",
                flow_amount=40.0,
                flow_offset="12346",
                strict=True,
            )

        self.assertAlmostEqual(peak, 150.0)
        self.assertAlmostEqual(legacy.drawdown, 0.20)
        self.assertAlmostEqual(risk.professional_snapshot.drawdown, 0.20)

    async def test_malformed_durable_state_fails_closed(self):
        with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value="nan")), \
             patch("bot.drawdown_persistence.db.save_key_value", AsyncMock(return_value=True)):
            with self.assertRaises(db.PersistenceError):
                await restore_update_real_account_peak(self.risk, 100.0, strict=True)

    async def test_persistence_read_failure_propagates(self):
        with patch(
            "bot.drawdown_persistence.db.load_key_value",
            AsyncMock(side_effect=db.PersistenceError("offline")),
        ):
            with self.assertRaises(db.PersistenceError):
                await restore_update_real_account_peak(self.risk, 100.0, strict=True)

    async def test_nonfinite_or_nonpositive_equity_rejected(self):
        for value in (0.0, -1.0, math.inf, math.nan):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    await restore_update_real_account_peak(self.risk, value, strict=True)


class RiskManagerV3PeakRestoreTests(unittest.TestCase):
    def test_restore_peak_does_not_confirm_capital(self):
        risk = RiskManagerV3()
        self.assertFalse(risk.confirmed)
        self.assertEqual(risk.restore_peak_equity(100.0), 100.0)
        self.assertFalse(risk.confirmed)

        risk.update_capital(CapitalState(80.0, 60.0))
        self.assertAlmostEqual(risk.drawdown, 0.20)

    def test_restore_peak_never_lowers_existing_high(self):
        risk = RiskManagerV3()
        risk.restore_peak_equity(100.0)
        self.assertEqual(risk.restore_peak_equity(90.0), 100.0)

    def test_cashflow_rebase_can_lower_peak_without_confirming_capital(self):
        risk = RiskManagerV3()
        risk.restore_peak_equity(100.0)
        self.assertFalse(risk.confirmed)
        self.assertEqual(risk.rebase_peak_equity(75.0), 75.0)
        self.assertFalse(risk.confirmed)

    def test_invalid_peak_rejected(self):
        risk = RiskManagerV3()
        for value in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    risk.restore_peak_equity(value)
                with self.assertRaises(ValueError):
                    risk.rebase_peak_equity(value)


if __name__ == "__main__":
    unittest.main()
