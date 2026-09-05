"""Deterministic regression test for PAPER wallet restart persistence.

This test performs no exchange I/O and cannot place orders. It verifies the
state transition that matters for deploy/restart safety:

    seed -> virtual balance change -> persist -> new process object -> restore

LIVE mode, trading gates, leverage and risk parameters are not modified.
"""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace

from bot import database as db
from bot import paper_wallet
from bot.engine import TradingEngine
from bot.logger import log


class _FakeRisk:
    def __init__(self, balance=0.0, peak=0.0):
        self.balance = float(balance)
        self.peak_balance = float(peak)
        self.drawdown = 0.0
        self.balance_confirmed = self.balance > 0

    def update(self, balance):
        balance = float(balance)
        self.balance = balance
        if balance > self.peak_balance:
            self.peak_balance = balance
        self.drawdown = (
            (self.peak_balance - balance) / self.peak_balance
            if self.peak_balance > 0 else 0.0
        )


class _FakeDailyTracker:
    def __init__(self):
        self.daily_target = 0.0
        self.daily_stop_loss = 999.0
        self.last_balance = None

    def recalc_limits(self, balance):
        self.last_balance = float(balance)


def _engine_like(balance=20.0, peak=20.0):
    return SimpleNamespace(
        paper_trade=True,
        _paper_balance=float(balance),
        risk=_FakeRisk(balance, peak),
        daily_tracker=_FakeDailyTracker(),
        daily_target=0.0,
        daily_stop_loss=999.0,
    )


class PaperWalletRestartTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Idempotent: sitecustomize may already have installed the patch.
        paper_wallet.install(log)
        self._orig_save = db.save_key_value
        self._orig_load = db.load_key_value
        self.store = {}

        async def _save(key, value):
            self.store[key] = value

        async def _load(key):
            return self.store.get(key)

        db.save_key_value = _save
        db.load_key_value = _load

    async def asyncTearDown(self):
        db.save_key_value = self._orig_save
        db.load_key_value = self._orig_load

    async def test_balance_and_peak_survive_restart(self):
        first_process = _engine_like(balance=20.0, peak=20.0)

        # Simulate a PAPER loss. This only mutates the virtual wallet object.
        TradingEngine._paper_apply_virtual_balance(
            first_process, 14.50, "test_simulated_paper_loss"
        )
        self.assertAlmostEqual(first_process._paper_balance, 14.50)
        self.assertAlmostEqual(first_process.risk.peak_balance, 20.00)
        self.assertAlmostEqual(first_process.risk.drawdown, 0.275)

        # Persist explicitly so the test is deterministic even though the
        # production path also schedules persistence after every wallet change.
        await TradingEngine._paper_persist_wallet(
            first_process, "test_before_restart"
        )

        raw = self.store.get("paper_wallet_state_v1")
        self.assertIsNotNone(raw)
        saved = json.loads(raw)
        self.assertEqual(saved["version"], 1)
        self.assertAlmostEqual(saved["balance"], 14.50)
        self.assertAlmostEqual(saved["peak_balance"], 20.00)

        # New object = new process/deploy. Its arbitrary pre-restore values
        # must be replaced by the persisted PAPER state.
        restarted_process = _engine_like(balance=99.0, peak=99.0)
        restored = await TradingEngine._paper_restore_wallet_from_db(
            restarted_process
        )

        self.assertTrue(restored)
        self.assertAlmostEqual(restarted_process._paper_balance, 14.50)
        self.assertAlmostEqual(restarted_process.risk.balance, 14.50)
        self.assertAlmostEqual(restarted_process.risk.peak_balance, 20.00)
        self.assertAlmostEqual(restarted_process.risk.drawdown, 0.275)
        self.assertTrue(restarted_process.risk.balance_confirmed)
        self.assertAlmostEqual(restarted_process.daily_stop_loss, 0.0)
        self.assertAlmostEqual(
            restarted_process.daily_tracker.daily_stop_loss, 0.0
        )

    async def test_invalid_persisted_state_is_fail_safe(self):
        self.store["paper_wallet_state_v1"] = json.dumps({
            "version": 1,
            "balance": 25.0,
            "peak_balance": 20.0,
        })
        process = _engine_like(balance=20.0, peak=20.0)
        before = process._paper_balance

        restored = await TradingEngine._paper_restore_wallet_from_db(process)

        self.assertFalse(restored)
        self.assertEqual(process._paper_balance, before)

    async def test_viability_uses_persisted_wallet_not_small_real_balance(self):
        self.store["paper_wallet_state_v1"] = json.dumps({
            "version": 1,
            "balance": 14.50,
            "peak_balance": 20.0,
        })
        process = SimpleNamespace(
            paper_trade=True,
            risk=_FakeRisk(balance=0.0038, peak=0.0038),
            daily_tracker=_FakeDailyTracker(),
            daily_target=0.0,
            daily_stop_loss=999.0,
            instruments={
                "BTCUSDT": {"minQty": 1, "multiplier": 0.001},
            },
            viable_symbols=[],
            client=SimpleNamespace(
                get_all_tickers=AsyncMock(return_value=[{
                    "symbol": "BTCUSDT", "lastPrice": "50000"
                }]),
                get_cached_ticker=lambda _symbol: None,
            ),
        )

        ok = await TradingEngine._filter_viable_symbols(process)

        self.assertTrue(ok)
        self.assertEqual(process.viable_symbols, ["BTCUSDT"])
        self.assertAlmostEqual(process._paper_balance, 14.50)
        self.assertAlmostEqual(process.risk.balance, 14.50)
        self.assertAlmostEqual(process.risk.peak_balance, 20.0)


if __name__ == "__main__":
    unittest.main()
