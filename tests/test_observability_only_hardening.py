import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot import final_loss_budget as loss_budget
from bot import final_sizing_invariants as final_sizing
from bot import pilot_risk_cap_hardening as pilot_cap
from bot import pullback_confirmation_hardening as pullback
from bot.config import cfg


class _Log:
    def __init__(self):
        self.rows = []

    def _add(self, level, *args):
        self.rows.append((level, args))

    def info(self, *args, **kwargs):
        self._add("INFO", *args)

    def warning(self, *args, **kwargs):
        self._add("WARNING", *args)

    def critical(self, *args, **kwargs):
        self._add("CRITICAL", *args)

    def rendered(self):
        out = []
        for level, args in self.rows:
            fmt = args[0]
            try:
                msg = fmt % tuple(args[1:]) if len(args) > 1 else str(fmt)
            except Exception:
                msg = str(args)
            out.append((level, msg))
        return out


def _strategy_signal(entry_type="PULLBACK"):
    sig = SimpleNamespace(
        symbol="ADAUSDT",
        direction="LONG",
        entry_type=entry_type,
        entry=100.0,
        sl=99.12,
        tp=102.0,
        score=69,
        regime="TRENDING_UP",
        _bgx_atr_15m=0.40,
        _bgx_atr_1h=0.60,
        _bgx_adjusted_atr=0.44,
        _bgx_formation_timestamp=1800000000.0,
        _bgx_formation_bucket=2000000,
        _bgx_4h_bias="LONG",
        _bgx_1h_bias="LONG",
        _bgx_15m_bias="LONG",
    )
    return sig


class StrategyStopGeometryObservabilityTests(unittest.TestCase):
    def setUp(self):
        pullback._STRATEGY_STOP_GEOMETRY_LAST.clear()

    def test_pullback_block_still_emits_complete_geometry_and_linked_setup_id(self):
        sig = _strategy_signal("PULLBACK")

        class Analyzer:
            def analyze_mtf(self, *args, **kwargs):
                return sig

        metrics = {
            "ok": False,
            "reason": "opposite_structure_not_reversed",
            "votes": {"rsi_recovered": True},
            "vote_count": 1,
            "structure": "DOWNTREND",
            "bos": False,
            "bos_dir": "NONE",
            "rsi": 45.0,
            "macd_hist": -0.1,
            "macd_prev": -0.05,
            "ema20_side": False,
        }
        log = _Log()
        with patch("bot.pullback_confirmation_hardening.time.time", return_value=1800000000.0), \
             patch.object(pullback, "_pullback_metrics", return_value=metrics):
            pullback.install(Analyzer, log)
            result = Analyzer().analyze_mtf("ADAUSDT", [{}] * 40, [], [])

        self.assertIsNone(result)
        rendered = [m for _, m in log.rendered()]
        geometry = [m for m in rendered if "[STRATEGY_STOP_GEOMETRY]" in m]
        blocked = [m for m in rendered if "[PULLBACK_CONFIRMATION]" in m and "result=BLOCKED" in m]
        self.assertEqual(len(geometry), 1)
        self.assertEqual(len(blocked), 1)
        setup_id = sig._bgx_setup_id
        self.assertIn(f"setup_id={setup_id}", geometry[0])
        self.assertIn(f"setup_id={setup_id}", blocked[0])
        self.assertIn("atr_15m=0.4", geometry[0])
        self.assertIn("atr_1h=0.6", geometry[0])
        self.assertIn("adjusted_atr=0.44", geometry[0])
        self.assertIn("original_stop_pct=0.88000000", geometry[0])
        self.assertIn("target_pct=2.00000000", geometry[0])

    def test_same_setup_is_deduplicated_without_random_identity(self):
        sig = _strategy_signal("MOMENTUM")

        class Analyzer:
            def analyze_mtf(self, *args, **kwargs):
                return sig

        log = _Log()
        with patch("bot.pullback_confirmation_hardening.time.time", return_value=1800000000.0):
            pullback.install(Analyzer, log)
            a = Analyzer()
            self.assertIs(a.analyze_mtf("ADAUSDT", [], [], []), sig)
            self.assertIs(a.analyze_mtf("ADAUSDT", [], [], []), sig)

        geometry = [m for _, m in log.rendered() if "[STRATEGY_STOP_GEOMETRY]" in m]
        self.assertEqual(len(geometry), 1)
        self.assertEqual(sig._bgx_setup_id, "ADAUSDT:LONG:MOMENTUM:2000000")

    def test_geometry_event_covers_all_entry_types(self):
        for idx, entry_type in enumerate(("BOS_BREAK", "MOMENTUM", "PULLBACK")):
            pullback._STRATEGY_STOP_GEOMETRY_LAST.clear()
            sig = _strategy_signal(entry_type)

            class Analyzer:
                def analyze_mtf(self, *args, **kwargs):
                    return sig

            log = _Log()
            with patch(
                "bot.pullback_confirmation_hardening.time.time",
                return_value=1800000000.0 + idx * 900,
            ), patch.object(
                pullback, "_pullback_metrics",
                return_value={
                    "ok": True, "votes": {}, "vote_count": 5,
                    "structure": "UPTREND", "bos": True,
                    "bos_dir": "BULLISH", "rsi": 60,
                },
            ):
                pullback.install(Analyzer, log)
                result = Analyzer().analyze_mtf("ADAUSDT", [{}] * 40, [], [])
            self.assertIs(result, sig)
            geometry = [m for _, m in log.rendered() if "[STRATEGY_STOP_GEOMETRY]" in m]
            self.assertEqual(len(geometry), 1)
            self.assertIn(f"entry_type={entry_type}", geometry[0])


class FinalLossObservabilityTests(unittest.TestCase):
    """Telemetry for the equity-based loss budget (budget = equity * risk_pct)."""

    def test_block_exposes_equity_normalized_arithmetic(self):
        metrics = loss_budget.measure(5, 100, 99.12, "LONG", 50, .0032, equity=100, risk_pct=.01)
        self.assertAlmostEqual(metrics["stop_fraction"] * 100, .88, places=8)
        self.assertAlmostEqual(metrics["projected_loss"], 6.0, places=8)
        self.assertAlmostEqual(metrics["loss_limit"], 1.0, places=8)
        self.assertAlmostEqual(metrics["projected_loss_pct_equity"], 6.0, places=8)
        self.assertAlmostEqual(metrics["headroom_usdt"], -5.0, places=8)
        with self.assertRaisesRegex(ValueError, "projected loss exceeds equity risk budget"):
            loss_budget.validate(5, 100, 99.12, "LONG", 50, .0032, equity=100, risk_pct=.01)

        log = _Log()
        loss_budget.emit_telemetry(
            log, symbol="ADAUSDT", setup_id="ADAUSDT:LONG:MOMENTUM:1",
            stage="FINAL_SIZING_INVARIANT", qty=5, entry=100, stop=99.12,
            direction="LONG", leverage=50, cost_fraction=.0032,
            result="BLOCK",
            specific_reason="projected_loss_exceeds_equity_risk_budget",
            equity=100, risk_pct=.01, risk_v3_qty=.25,
        )
        msg = [m for _, m in log.rendered() if "[FINAL_LOSS_BUDGET]" in m][0]
        self.assertIn("result=BLOCK", msg)
        self.assertIn("specific_reason=projected_loss_exceeds_equity_risk_budget", msg)
        self.assertIn("qty_authority=RISK_POLICY_MIN_OF_CAPS", msg)
        self.assertIn("risk_v3_qty=0.25", msg)
        self.assertIn("budget_basis=EQUITY_RISK_PCT", msg)
        self.assertIn("allowed_loss_pct_equity=1.00000000", msg)

    def test_pass_exposes_positive_headroom(self):
        metrics = loss_budget.measure(0.1, 100, 99.5027, "LONG", 50, .0022, equity=100, risk_pct=.01)
        self.assertGreater(metrics["headroom_usdt"], 0)
        before = loss_budget.validate(0.1, 100, 99.5027, "LONG", 50, .0022, equity=100, risk_pct=.01)
        log = _Log()
        loss_budget.emit_telemetry(
            log, symbol="BTCUSDT", setup_id="BTCUSDT:LONG:MOMENTUM:1",
            stage="FRESH_PREDISPATCH_RECHECK", qty=0.1, entry=100, stop=99.5027,
            direction="LONG", leverage=50, cost_fraction=.0022,
            result="PASS", specific_reason="within_equity_risk_budget",
            equity=100, risk_pct=.01,
        )
        after = loss_budget.validate(0.1, 100, 99.5027, "LONG", 50, .0022, equity=100, risk_pct=.01)
        self.assertEqual(before, after)
        msg = [m for _, m in log.rendered() if "[FINAL_LOSS_BUDGET]" in m][0]
        self.assertIn("stage=FRESH_PREDISPATCH_RECHECK", msg)
        self.assertIn("result=PASS", msg)

    def test_invalid_telemetry_input_is_reported_not_swallowed(self):
        log = _Log()
        loss_budget.emit_telemetry(
            log, symbol="BTCUSDT", setup_id="x", stage="FINAL_SIZING_INVARIANT",
            qty=float("nan"), entry=100, stop=99, direction="LONG", leverage=50,
            cost_fraction=.0022, result="BLOCK", specific_reason="nonfinite_loss_budget",
            equity=100, risk_pct=.01,
        )
        msg = [m for _, m in log.rendered() if "[FINAL_LOSS_BUDGET]" in m][0]
        self.assertIn("telemetry_error=ValueError", msg)
        self.assertIn("specific_reason=nonfinite_loss_budget", msg)

    def test_logging_failure_propagates_instead_of_being_silently_dropped(self):
        class BrokenLog:
            def info(self, *args, **kwargs):
                raise RuntimeError("logging down")
            def warning(self, *args, **kwargs):
                raise RuntimeError("logging down")

        expected = loss_budget.validate(0.1, 100, 99.5027, "LONG", 50, .0022, equity=100, risk_pct=.01)
        with self.assertRaises(RuntimeError):
            loss_budget.emit_telemetry(
                BrokenLog(), symbol="BTCUSDT", setup_id="x",
                stage="FINAL_SIZING_INVARIANT", qty=0.1, entry=100, stop=99.5027,
                direction="LONG", leverage=50, cost_fraction=.0022,
                result="PASS", specific_reason="within_equity_risk_budget",
                equity=100, risk_pct=.01,
            )
        self.assertEqual(
            loss_budget.validate(0.1, 100, 99.5027, "LONG", 50, .0022, equity=100, risk_pct=.01),
            expected,
        )


class FinalSizingTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.old = {k: getattr(cfg, k) for k in ("LEVERAGE", "MAX_RISK_PCT", "MAX_MARGIN_PCT")}
        cfg.LEVERAGE = 50
        cfg.MAX_RISK_PCT = 0.01
        cfg.MAX_MARGIN_PCT = 0.50

    def tearDown(self):
        for k, v in self.old.items():
            setattr(cfg, k, v)

    def _exercise(self, stop, *, risk_qty=.25, confirmed=True):
        from bot.professional_risk import CapitalState

        class EngineModule:
            pass

        info = {"multiplier": "0.001", "lotSize": "1", "minQty": "1", "minNotional": "0"}
        EngineModule.minimum_base_quantity = lambda info, price: .001
        EngineModule._final_sizing_invariants_installed = False
        snapshot = SimpleNamespace(
            capital=CapitalState(equity=20.0, available_collateral=20.0), confirmed=confirmed,
        )
        risk = SimpleNamespace(size=lambda *args, **kwargs: risk_qty, professional_snapshot=snapshot)
        engine = SimpleNamespace(
            paper_trade=False,
            pilot=SimpleNamespace(enabled=True),
            _pilot_available_balance=20.0,
            risk=risk,
            instruments={"TESTUSDT": info},
            positions={},
        )
        signal = SimpleNamespace(
            sl=stop, direction="LONG",
            _bgx_setup_id="TESTUSDT:LONG:MOMENTUM:1",
        )
        log = _Log()
        final_sizing.install(EngineModule, pilot_cap, log)
        token_engine = pilot_cap._PILOT_ENGINE.set(engine)
        token_symbol = pilot_cap._PILOT_SYMBOL.set("TESTUSDT")
        token_qty = pilot_cap._PILOT_FINAL_QTY.set(None)
        token_signal = pilot_cap._PILOT_SIGNAL.set(signal)
        try:
            qty = EngineModule.minimum_base_quantity(info, 100.0)
            stored = pilot_cap._PILOT_FINAL_QTY.get()
        finally:
            pilot_cap._PILOT_SIGNAL.reset(token_signal)
            pilot_cap._PILOT_FINAL_QTY.reset(token_qty)
            pilot_cap._PILOT_SYMBOL.reset(token_symbol)
            pilot_cap._PILOT_ENGINE.reset(token_engine)
        return qty, stored, log

    def test_final_sizing_block_logs_reason_and_setup(self):
        qty, stored, log = self._exercise(99.6, confirmed=False)
        self.assertEqual(qty, 0.0)
        self.assertEqual(stored, 0.0)
        msg = [m for _, m in log.rendered() if "[FINAL_SIZING_INVARIANT]" in m][-1]
        self.assertIn("result=BLOCK", msg)
        self.assertIn("setup_id=TESTUSDT:LONG:MOMENTUM:1", msg)
        self.assertIn("reason=capital_snapshot_unconfirmed", msg)

    def test_final_sizing_pass_is_risk_authoritative(self):
        qty, stored, log = self._exercise(99.6)
        self.assertGreater(qty, 0.0)
        self.assertLessEqual(qty, .25)
        self.assertEqual(qty, stored)
        msg = [m for _, m in log.rendered() if "[FINAL_LOSS_BUDGET]" in m][0]
        self.assertIn("stage=FINAL_SIZING_INVARIANT", msg)
        self.assertIn("result=PASS", msg)
        self.assertIn("budget_basis=EQUITY_RISK_PCT", msg)


if __name__ == "__main__":
    unittest.main()
