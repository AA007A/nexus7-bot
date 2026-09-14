import unittest
from types import SimpleNamespace

from bot.config import cfg
from bot import final_sizing_invariants as final_sizing
from bot import pilot_risk_cap_hardening as pilot_cap


class _Log:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _EngineModule:
    pass


class FinalSizingInvariantTests(unittest.TestCase):
    def setUp(self):
        self.old_leverage = cfg.LEVERAGE
        cfg.LEVERAGE = 50

    def tearDown(self):
        cfg.LEVERAGE = self.old_leverage

    def _install(self, *, risk_size, target_qty=5.0, available=20.0):
        module = _EngineModule()
        module.minimum_base_quantity = lambda info, price: target_qty
        module._final_sizing_invariants_installed = False
        risk = SimpleNamespace(size=risk_size)
        engine = SimpleNamespace(
            paper_trade=False,
            pilot=SimpleNamespace(enabled=True),
            _pilot_available_balance=available,
            risk=risk,
            instruments={},
            positions={},
        )
        final_sizing.install(module, pilot_cap, _Log())
        return module, engine

    def _call(self, module, engine, price=100.0):
        token_engine = pilot_cap._PILOT_ENGINE.set(engine)
        token_symbol = pilot_cap._PILOT_SYMBOL.set("TESTUSDT")
        token_qty = pilot_cap._PILOT_FINAL_QTY.set(None)
        try:
            qty = module.minimum_base_quantity({}, price)
            stored = pilot_cap._PILOT_FINAL_QTY.get()
            return qty, stored
        finally:
            pilot_cap._PILOT_FINAL_QTY.reset(token_qty)
            pilot_cap._PILOT_SYMBOL.reset(token_symbol)
            pilot_cap._PILOT_ENGINE.reset(token_engine)

    def test_risk_quantity_is_final_cap_below_operator_target(self):
        module, engine = self._install(risk_size=lambda *a, **k: 0.25)
        qty, stored = self._call(module, engine)
        self.assertAlmostEqual(qty, 0.25)
        self.assertAlmostEqual(stored, 0.25)

    def test_operator_50pct_margin_target_remains_cap_when_risk_allows_more(self):
        module, engine = self._install(risk_size=lambda *a, **k: 10.0)
        qty, stored = self._call(module, engine)
        self.assertAlmostEqual(qty, 5.0)
        self.assertAlmostEqual(stored, 5.0)
        # available=20; 50%=10 margin; 5 qty*$100/50x=$10 margin.
        self.assertAlmostEqual((qty * 100.0) / cfg.LEVERAGE, 10.0)

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

    def test_margin_above_50pct_target_fails_closed(self):
        # Deliberately inconsistent upstream target: 6 qty at $100/50x = $12,
        # above the $10 operator margin cap. Risk allows it, final invariant blocks.
        module, engine = self._install(
            risk_size=lambda *a, **k: 10.0,
            target_qty=6.0,
        )
        qty, stored = self._call(module, engine)
        self.assertEqual(qty, 0.0)
        self.assertEqual(stored, 0.0)


if __name__ == "__main__":
    unittest.main()
