import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import pilot_live_runtime as live
from bot.engine import TradingEngine
from bot.financial_state import validate_financial_state


class _Risk:
    def __init__(self):
        self._ready = False
        self.balance = 0.0
        self.peak_balance = 0.0
        self.drawdown = 0.0
        self.balance_confirmed = False

    def init(self, value):
        self._ready = True
        self.balance = float(value)
        self.peak_balance = float(value)
        self.drawdown = 0.0

    def update(self, value):
        value = float(value)
        self.balance = value
        self.peak_balance = max(self.peak_balance, value)
        self.drawdown = (
            (self.peak_balance - value) / self.peak_balance
            if self.peak_balance > 0 else 0.0
        )


class _Integrity:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.assess = AsyncMock(return_value=None)

    def can_open_new(self):
        return self.allowed

    def block_reason(self):
        return "blocked"


class _Log:
    info = staticmethod(lambda *a, **k: None)
    warning = staticmethod(lambda *a, **k: None)
    critical = staticmethod(lambda *a, **k: None)


class PilotLiveRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_uses_equity_for_risk_and_tracks_available_separately(self):
        engine = SimpleNamespace(
            client=SimpleNamespace(),
            risk=_Risk(),
        )
        state = {
            "equity": 100.0,
            "available": 25.0,
            "available_source": "availableMargin",
            "accountEquity": "100",
            "availableMargin": "25",
        }
        with patch.object(
            live.account_semantics, "read_account_state", AsyncMock(return_value=state)
        ), patch.object(
            live.capital_flows,
            "reconcile_external_capital_flows",
            AsyncMock(return_value={"applied": 0, "bootstrap": False}),
        ) as reconcile, patch.object(
            live, "restore_update_real_account_peak", AsyncMock(return_value=None)
        ):
            out = await live._refresh_account(engine, _Log())

        self.assertIs(out, state)
        reconcile.assert_awaited_once_with(engine.client, engine.risk, 100.0, strict=True)
        self.assertEqual(engine.risk.balance, 100.0)
        self.assertEqual(engine._pilot_account_equity, 100.0)
        self.assertEqual(engine._pilot_available_balance, 25.0)
        self.assertEqual(
            engine.client._last_account_overview_snapshot["availableMargin"], "25"
        )
        self.assertIn("_observed_at", engine.client._last_account_overview_snapshot)

    async def test_preflight_block_prevents_original_open(self):
        class Engine:
            _pilot_live_runtime_patched = False

            def __init__(self):
                self.paper_trade = False
                self.client = SimpleNamespace()
                self.risk = _Risk()
                self.integrity = _Integrity()
                self.instruments = {"BTCUSDT": {}}

            async def _connect(self):
                self.connected = True

            async def _update_balance(self):
                return None

            async def _refresh_entry_balance(self):
                return True

            async def _open(self, sig):
                self.original_open_called = True
                return "opened"

        live.install(Engine, _Log())
        engine = Engine()
        engine.original_open_called = False
        with patch.object(
            live, "_run_readonly_preflight", AsyncMock(return_value=False)
        ):
            result = await engine._open(SimpleNamespace(symbol="BTCUSDT"))

        self.assertIsNone(result)
        self.assertFalse(engine.original_open_called)
        engine.integrity.assess.assert_not_awaited()

    async def test_integrity_block_prevents_original_open(self):
        class Engine:
            _pilot_live_runtime_patched = False

            def __init__(self):
                self.paper_trade = False
                self.client = SimpleNamespace()
                self.risk = _Risk()
                self.integrity = _Integrity(allowed=False)
                self.instruments = {"BTCUSDT": {}}

            async def _connect(self):
                self.connected = True

            async def _update_balance(self):
                return None

            async def _refresh_entry_balance(self):
                return True

            async def _open(self, sig):
                self.original_open_called = True
                return "opened"

        live.install(Engine, _Log())
        engine = Engine()
        engine.original_open_called = False
        with patch.object(
            live, "_run_readonly_preflight", AsyncMock(return_value=True)
        ):
            result = await engine._open(SimpleNamespace(symbol="BTCUSDT"))

        self.assertIsNone(result)
        self.assertFalse(engine.original_open_called)
        engine.integrity.assess.assert_awaited_once()

    async def test_entry_refresh_keeps_equity_authoritative_during_open(self):
        class Engine:
            _pilot_live_runtime_patched = False

            def __init__(self):
                self.paper_trade = False
                self.client = SimpleNamespace()
                self.risk = _Risk()
                self.risk.init(100.0)
                self.integrity = _Integrity()
                self.instruments = {"BTCUSDT": {}}
                self._pilot_open_in_progress = True
                self._pilot_available_balance = 25.0

            async def _connect(self):
                self.connected = True

            async def _update_balance(self):
                return None

            async def _refresh_entry_balance(self):
                return True

            async def _open(self, sig):
                return None

        live.install(Engine, _Log())
        engine = Engine()
        state = {
            "equity": 100.0,
            "available": 25.0,
            "available_source": "availableMargin",
        }
        with patch.object(
            live, "_refresh_account", AsyncMock(return_value=state)
        ):
            ok = await engine._refresh_entry_balance()

        self.assertTrue(ok)
        self.assertEqual(engine.risk.balance, 100.0)
        self.assertEqual(engine._pilot_available_balance, 25.0)
        self.assertTrue(engine.risk.balance_confirmed)

    async def test_historical_alias_transition_is_eliminated_at_source(self):
        class Engine:
            _pilot_live_runtime_patched = False

            def __init__(self):
                self.paper_trade = False
                self.client = SimpleNamespace()
                self.risk = _Risk()
                self.risk._ready = True
                self.risk.balance = 21.5075351411
                self.risk.peak_balance = 63.7942573
                self.risk.drawdown = 0.6628609525155488
                self.integrity = _Integrity()
                self.instruments = {"DOGEUSDT": {}}
                self._pilot_open_in_progress = True
                self._pilot_available_balance = 11.9815151411

            async def _connect(self):
                self.connected = True

            async def _update_balance(self):
                return None

            async def _refresh_entry_balance(self):
                return True

            async def _open(self, sig):
                return None

        live.install(Engine, _Log())
        engine = Engine()
        state = {
            "equity": 21.5075351411,
            "available": 11.9815151411,
            "available_source": "availableMargin",
        }
        with patch.object(
            live, "_refresh_account", AsyncMock(return_value=state)
        ):
            ok = await engine._refresh_entry_balance()

        self.assertTrue(ok)
        self.assertAlmostEqual(engine.risk.balance, 21.5075351411)
        self.assertAlmostEqual(engine._pilot_available_balance, 11.9815151411)
        validate_financial_state(
            equity=engine.risk.balance,
            available_margin=engine._pilot_available_balance,
            hwm=engine.risk.peak_balance,
            drawdown=engine.risk.drawdown,
        )

    def test_core_affordability_uses_pilot_collateral_without_relabeling_equity(self):
        engine = TradingEngine.__new__(TradingEngine)
        engine.pilot = SimpleNamespace(enabled=True)
        engine.risk = SimpleNamespace(balance=21.5075351411)
        engine._pilot_available_balance = 11.9815151411
        self.assertAlmostEqual(engine._entry_available_funds(), 11.9815151411)
        self.assertAlmostEqual(engine.risk.balance, 21.5075351411)

    def test_core_affordability_falls_back_to_risk_balance_outside_pilot(self):
        engine = TradingEngine.__new__(TradingEngine)
        engine.pilot = SimpleNamespace(enabled=False)
        engine.risk = SimpleNamespace(balance=21.5075351411)
        self.assertAlmostEqual(engine._entry_available_funds(), 21.5075351411)

if __name__ == "__main__":
    unittest.main()
