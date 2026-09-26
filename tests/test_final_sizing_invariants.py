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


INFO = {
    "multiplier": "0.001",
    "lotSize": "1",
    "minQty": "1",
    "minNotional": "0",
}


class FinalSizingInvariantTests(unittest.TestCase):
    def setUp(self):
        self.old_leverage = cfg.LEVERAGE
        cfg.LEVERAGE = 50

    def tearDown(self):
        cfg.LEVERAGE = self.old_leverage

    def _install(self, *, risk_size, legacy_qty=0.001, available=20.0):
        module = _EngineModule()
        # Deliberately tiny legacy result: final authority must not inherit it.
        module.minimum_base_quantity = lambda info, price: legacy_qty
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

    def _call(self, module, engine, price=100.0, info=INFO):
        token_engine = pilot_cap._PILOT_ENGINE.set(engine)
        token_symbol = pilot_cap._PILOT_SYMBOL.set("TESTUSDT")
        token_qty = pilot_cap._PILOT_FINAL_QTY.set(None)
        token_signal = pilot_cap._PILOT_SIGNAL.set(SimpleNamespace(sl=price * .996, direction='LONG'))
        try:
            qty = module.minimum_base_quantity(info, price)
            stored = pilot_cap._PILOT_FINAL_QTY.get()
            return qty, stored
        finally:
            pilot_cap._PILOT_SIGNAL.reset(token_signal)
            pilot_cap._PILOT_FINAL_QTY.reset(token_qty)
            pilot_cap._PILOT_SYMBOL.reset(token_symbol)
            pilot_cap._PILOT_ENGINE.reset(token_engine)

    # Contract change (2026-09-26 audit P0-1). Previously the operator margin
    # target was returned even when RiskManagerV3 sized less (qty=5 vs 0.25),
    # i.e. the stop-risk budget was not enforced. The executed contract is now
    # final_qty = min(stop_risk_qty, operator_margin_cap_qty).
    def test_cap_is_derived_from_50pct_margin_not_legacy_quantity(self):
        module, engine = self._install(risk_size=lambda *a, **k: 10.0, legacy_qty=0.001)
        qty, stored = self._call(module, engine)
        # available=20, margin=10, leverage=50 => notional=500; price=100 => cap qty=5.
        self.assertAlmostEqual(qty, 5.0)
        self.assertAlmostEqual(stored, 5.0)
        self.assertAlmostEqual((qty * 100.0) / cfg.LEVERAGE, 10.0)

    def test_stop_risk_quantity_binds_below_operator_cap(self):
        module, engine = self._install(risk_size=lambda *a, **k: 0.25)
        qty, stored = self._call(module, engine)
        self.assertAlmostEqual(qty, 0.25)
        self.assertAlmostEqual(stored, 0.25)

    def test_operator_cap_binds_when_risk_allows_more(self):
        module, engine = self._install(risk_size=lambda *a, **k: 10.0)
        qty, stored = self._call(module, engine)
        self.assertAlmostEqual(qty, 5.0)
        self.assertAlmostEqual(stored, 5.0)

    def test_contract_floor_never_exceeds_50pct_margin(self):
        module, engine = self._install(risk_size=lambda *a, **k: 10.0, available=19.37)
        qty, _ = self._call(module, engine, price=2.0)
        margin = qty * 2.0 / cfg.LEVERAGE
        self.assertLessEqual(margin, 19.37 * 0.50 + 1e-9)
        self.assertGreater(qty, 0.0)

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


if __name__ == "__main__":
    unittest.main()
