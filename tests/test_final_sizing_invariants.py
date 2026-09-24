"""Final LIVE sizing is risk-authoritative (minimum of all caps).

Formerly these tests asserted that the operator 50%-margin target was the
quantity authority and that RiskManagerV3 "does not shrink" it. That is the
P0 defect fixed here; the tests now assert the opposite invariant.
"""
import unittest
from types import SimpleNamespace

from bot.config import cfg
from bot import final_sizing_invariants as final_sizing
from bot import pilot_risk_cap_hardening as pilot_cap
from bot.professional_risk import CapitalState


class _Log:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _EngineModule:
    pass


INFO = {
    "multiplier": "0.001",
    "lotSize": "1",
    "minQty": "1",
    "minNotional": "0",
}


class FinalSizingInvariantTests(unittest.TestCase):
    def setUp(self):
        self.old = {k: getattr(cfg, k) for k in ("LEVERAGE", "MAX_RISK_PCT", "MAX_MARGIN_PCT")}
        cfg.LEVERAGE = 50
        cfg.MAX_RISK_PCT = 0.01
        cfg.MAX_MARGIN_PCT = 0.50

    def tearDown(self):
        for k, v in self.old.items():
            setattr(cfg, k, v)

    def _install(self, *, risk_size, legacy_qty=0.001, available=20.0, equity=None, confirmed=True):
        module = _EngineModule()
        module.minimum_base_quantity = lambda info, price: legacy_qty
        module._final_sizing_invariants_installed = False
        snapshot = SimpleNamespace(
            capital=CapitalState(
                equity=available if equity is None else equity,
                available_collateral=available,
            ),
            confirmed=confirmed,
        )
        risk = SimpleNamespace(size=risk_size, professional_snapshot=snapshot)
        engine = SimpleNamespace(
            paper_trade=False,
            pilot=SimpleNamespace(enabled=True),
            _pilot_available_balance=available,
            risk=risk,
            instruments={"TESTUSDT": INFO},
            positions={},
        )
        final_sizing.install(module, pilot_cap, _Log())
        return module, engine

    def _call(self, module, engine, price=100.0, info=INFO, stop_pct=0.004):
        token_engine = pilot_cap._PILOT_ENGINE.set(engine)
        token_symbol = pilot_cap._PILOT_SYMBOL.set("TESTUSDT")
        token_qty = pilot_cap._PILOT_FINAL_QTY.set(None)
        token_signal = pilot_cap._PILOT_SIGNAL.set(
            SimpleNamespace(sl=price * (1 - stop_pct), direction='LONG'))
        try:
            qty = module.minimum_base_quantity(info, price)
            stored = pilot_cap._PILOT_FINAL_QTY.get()
            return qty, stored
        finally:
            pilot_cap._PILOT_SIGNAL.reset(token_signal)
            pilot_cap._PILOT_FINAL_QTY.reset(token_qty)
            pilot_cap._PILOT_SYMBOL.reset(token_symbol)
            pilot_cap._PILOT_ENGINE.reset(token_engine)

    def test_risk_quantity_shrinks_operator_target(self):
        module, engine = self._install(risk_size=lambda *a, **k: 0.25)
        qty, stored = self._call(module, engine)
        self.assertGreater(qty, 0.0)
        self.assertLessEqual(qty, 0.25)
        self.assertEqual(qty, stored)

    def test_operator_margin_is_not_a_utilization_target(self):
        module, engine = self._install(risk_size=lambda *a, **k: 10.0)
        qty, _ = self._call(module, engine)
        # Old behavior: exactly 5.0 (50% of 20 at 50x). Now the equity stop
        # budget (1% of 20 = 0.2 USDT) binds far below that.
        self.assertLess(qty, 5.0)
        stop_distance = 100.0 * 0.004
        self.assertLessEqual(qty * stop_distance, 20.0 * 0.01 + 1e-12)

    def test_legacy_minimum_quantity_is_not_inherited(self):
        module, engine = self._install(risk_size=lambda *a, **k: 10.0, legacy_qty=123.0)
        qty, _ = self._call(module, engine)
        self.assertLess(qty, 123.0)

    def test_contract_floor_never_exceeds_margin_cap(self):
        module, engine = self._install(risk_size=lambda *a, **k: 10.0, available=19.37)
        qty, _ = self._call(module, engine, price=2.0, info={**INFO, "multiplier": "1"}, stop_pct=0.05)
        margin = qty * 2.0 / cfg.LEVERAGE
        self.assertLessEqual(margin, 19.37 * 0.50 + 1e-9)

    def test_risk_sizing_exception_fails_closed(self):
        def _raise(*args, **kwargs):
            raise RuntimeError("risk unavailable")
        module, engine = self._install(risk_size=_raise)
        qty, stored = self._call(module, engine)
        self.assertEqual(qty, 0.0)
        self.assertEqual(stored, 0.0)

    def test_invalid_risk_quantity_fails_closed(self):
        module, engine = self._install(risk_size=lambda *a, **k: float("nan"))
        qty, stored = self._call(module, engine)
        self.assertEqual(qty, 0.0)
        self.assertEqual(stored, 0.0)

    def test_invalid_exchange_metadata_fails_closed(self):
        module, engine = self._install(risk_size=lambda *a, **k: 10.0)
        qty, stored = self._call(module, engine, info={})
        self.assertEqual(qty, 0.0)
        self.assertEqual(stored, 0.0)

    def test_unconfirmed_capital_fails_closed(self):
        module, engine = self._install(risk_size=lambda *a, **k: 0.25, confirmed=False)
        qty, stored = self._call(module, engine)
        self.assertEqual(qty, 0.0)
        self.assertEqual(stored, 0.0)

    def test_minimum_lot_above_budget_is_no_trade(self):
        # 1 contract of 1 unit @100 with a 5% stop loses 5 USDT >> 0.2 budget.
        module, engine = self._install(risk_size=lambda *a, **k: 10.0)
        qty, stored = self._call(module, engine, info={**INFO, "multiplier": "1"}, stop_pct=0.05)
        self.assertEqual(qty, 0.0)
        self.assertEqual(stored, 0.0)

    def test_leverage_does_not_change_final_quantity_when_risk_binds(self):
        results = []
        for leverage in (10, 50):
            cfg.LEVERAGE = leverage
            module, engine = self._install(risk_size=lambda *a, **k: 10.0, available=1000.0)
            results.append(self._call(module, engine, stop_pct=0.02)[0])
        self.assertGreater(results[0], 0.0)
        self.assertAlmostEqual(results[0], results[1])


if __name__ == "__main__":
    unittest.main()
