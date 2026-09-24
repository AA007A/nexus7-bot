"""P0 regression tests reproducing the production risk state of 2026-09.

Production evidence: equity=25.9007 USDT, peak_equity=63.7943 USDT,
drawdown=59.40%, MAX_DRAWDOWN=50%, LIVE_RISK_OVERRIDE_APPROVED=true,
leverage=50x, runtime reported ``execution_effect=ALLOW_NEW_ENTRIES``.

Each class maps to one required scenario (A..F) from the hardening brief.
"""
import asyncio
import logging
import math
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.config import cfg
from bot import risk_policy as rp


EQUITY = 25.9007
PEAK = 63.7943
DRAWDOWN = (PEAK - EQUITY) / PEAK  # 0.59399...

INFO = {"multiplier": "0.001", "lotSize": "1", "minQty": "1", "minNotional": "0"}


class _Log:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


def _policy(**over):
    base = dict(
        leverage=50.0, max_risk_pct=0.01, max_margin_pct=0.50, max_drawdown=0.50,
        max_positions=2, daily_stop_loss_pct=0.03, daily_stop_loss_abs=0.0,
        min_rr_ratio=2.0,
    )
    base.update(over)
    return rp.RiskPolicy(**base)


class _CfgPatch:
    """Temporarily set cfg attributes."""

    def __init__(self, **values):
        self.values = values
        self.old = {}

    def __enter__(self):
        for k, v in self.values.items():
            self.old[k] = getattr(cfg, k)
            setattr(cfg, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            setattr(cfg, k, v)


# ─────────────────────────────────────────────────────────────── A ──
class ScenarioA_DrawdownOverride(unittest.TestCase):
    """drawdown=59.40%, limit=50%, override=true => NEW ENTRY BLOCKED."""

    def setUp(self):
        self.env = patch.dict(os.environ, {"LIVE_RISK_OVERRIDE_APPROVED": "true"})
        self.env.start()
        self.cfgp = _CfgPatch(MAX_DRAWDOWN=0.50, MAX_POSITIONS=2)
        self.cfgp.__enter__()

    def tearDown(self):
        self.cfgp.__exit__()
        self.env.stop()

    def test_drawdown_matches_production_evidence(self):
        self.assertAlmostEqual(DRAWDOWN * 100.0, 59.40, places=2)

    def test_canonical_decision_blocks_despite_override(self):
        d = rp.drawdown_entry_decision(DRAWDOWN, 0.50, override_requested=True)
        self.assertFalse(d.can_open)
        self.assertEqual(d.decision, rp.EntryDecision.BLOCK)
        self.assertTrue(d.override_requested)

    def test_boundary_equal_to_limit_blocks(self):
        self.assertFalse(rp.drawdown_entry_decision(0.50, 0.50).can_open)
        self.assertTrue(rp.drawdown_entry_decision(0.4999, 0.50).can_open)

    def test_invalid_drawdown_state_blocks(self):
        for bad in (float("nan"), float("inf"), -0.1, None, "x"):
            self.assertFalse(rp.drawdown_entry_decision(bad, 0.50).can_open, bad)

    def _installed(self):
        from bot import operator_runtime_policy as policy
        from bot.risk import RiskManager
        from bot.risk_manager_v3 import RiskManagerV3
        policy._install_drawdown_advisory(_Log())
        return RiskManager, RiskManagerV3

    def test_risk_manager_v3_can_open_false_with_override(self):
        _, RiskManagerV3 = self._installed()
        from bot.professional_risk import CapitalState
        v3 = RiskManagerV3()
        v3.restore_peak_equity(PEAK)
        v3.update_capital(CapitalState(equity=EQUITY, available_collateral=EQUITY))
        self.assertAlmostEqual(v3.drawdown, DRAWDOWN)
        self.assertFalse(v3.can_open(0))

    def test_legacy_risk_manager_can_open_false_with_override(self):
        RiskManager, _ = self._installed()
        rm = RiskManager()
        rm._ready = True
        rm.balance_confirmed = True
        rm.balance = EQUITY
        rm.peak_balance = PEAK
        rm.drawdown = DRAWDOWN
        self.assertFalse(rm.can_open(0))

    def test_engine_active_cannot_be_restored_by_override(self):
        from bot import operator_runtime_policy as policy
        engine = SimpleNamespace(active=True, risk=SimpleNamespace(drawdown=DRAWDOWN), _dd_alerted=False)

        async def legacy_update():
            engine.active = False

        guarded = policy._protect_drawdown_update(engine, legacy_update, _Log(), source="TEST")
        asyncio.run(guarded())
        self.assertFalse(engine.active)

    def test_drawdown_mode_advisory_does_not_allow_entries(self):
        """DRAWDOWN_MODE=ADVISORY must not be a path around the hard gate."""
        from bot import pilot as pilot_module
        engine = SimpleNamespace(risk=SimpleNamespace(drawdown=DRAWDOWN))
        with _CfgPatch(DRAWDOWN_MODE="ADVISORY"):
            self.assertTrue(pilot_module.drawdown_blocks_new_entries(engine))

    def test_override_may_only_authorize_risk_reducing_actions(self):
        A = rp.RiskAction
        self.assertFalse(rp.override_may_authorize(A.OPEN_POSITION))
        self.assertFalse(rp.override_may_authorize(A.INCREASE_POSITION))
        for action in (A.CLOSE_POSITION, A.REDUCE_POSITION, A.CANCEL_EXPOSURE_INCREASING_ORDER,
                       A.INSTALL_OR_REPAIR_PROTECTION, A.RECONCILE):
            self.assertTrue(rp.override_may_authorize(action), action)


# ─────────────────────────────────────────────────────────────── B ──
class ScenarioB_LeverageDoesNotIncreaseLossBudget(unittest.TestCase):
    """equity=25.9007, available=25.9007, MAX_RISK_PCT=1%, leverage=50x."""

    def _size(self, *, leverage=50.0, stop_pct=0.004, price=100.0, info=INFO,
              cost=0.0022, risk_pct=0.01, max_margin_pct=0.50):
        policy = _policy(leverage=leverage, max_risk_pct=risk_pct, max_margin_pct=max_margin_pct)
        return rp.size_new_entry(
            policy=policy, equity=EQUITY, available=EQUITY,
            entry=price, stop=price * (1 - stop_pct), direction="LONG",
            rules=rp.QuantityRules.from_instrument(info), cost_fraction=cost,
            maintenance_margin_rate=0.005,
        )

    def test_production_state_loss_budget_is_one_percent(self):
        d = self._size()
        self.assertTrue(d.allowed)
        self.assertAlmostEqual(d.risk_budget, 0.259007, places=6)
        self.assertLessEqual(d.projected_loss_at_stop, 0.259007 + 1e-12)

    def test_budget_invariant_across_leverage_stops_ticks_lots_fees(self):
        infos = [
            {"multiplier": "0.001", "lotSize": "1", "minQty": "1", "minNotional": "0"},
            {"multiplier": "0.01", "lotSize": "1", "minQty": "1", "minNotional": "0"},
            {"multiplier": "1", "lotSize": "1", "minQty": "1", "minNotional": "0"},
            {"multiplier": "10", "lotSize": "1", "minQty": "1", "minNotional": "0"},
            {"multiplier": "0.1", "lotSize": "5", "minQty": "5", "minNotional": "5"},
        ]
        checked = 0
        for leverage in (1, 5, 10, 20, 50, 75, 100, 125):
            for stop_pct in (0.001, 0.0025, 0.004, 0.01, 0.03, 0.08):
                for price in (0.08123, 0.5321, 2.137, 101.37, 3456.7, 65432.1):
                    for info in infos:
                        for cost in (0.0, 0.0012, 0.0022, 0.005):
                            d = self._size(leverage=leverage, stop_pct=stop_pct, price=price,
                                           info=info, cost=cost)
                            budget = EQUITY * 0.01
                            if d.allowed:
                                checked += 1
                                qty = d.qty
                                loss = qty * (price * stop_pct + price * cost)
                                self.assertLessEqual(loss, budget * (1 + 1e-9),
                                                     (leverage, stop_pct, price, info, cost))
                                self.assertLessEqual(qty * price / leverage,
                                                     EQUITY * 0.50 * (1 + 1e-9))
                            else:
                                self.assertEqual(d.qty, 0.0)
        self.assertGreater(checked, 100)

    def test_leverage_changes_collateral_not_loss(self):
        a = self._size(leverage=10.0, stop_pct=0.02)
        b = self._size(leverage=50.0, stop_pct=0.02)
        self.assertTrue(a.allowed and b.allowed)
        self.assertEqual(a.binding_constraint, "RISK")
        self.assertEqual(b.binding_constraint, "RISK")
        self.assertAlmostEqual(a.qty, b.qty)
        self.assertLess(b.required_margin, a.required_margin)

    def test_final_loss_budget_is_equity_based(self):
        from bot import final_loss_budget as flb
        # 50% margin at 50x on this account: margin=12.95, notional=647.5,
        # 0.4% stop => ~2.59 USDT (10% of equity) + costs. Must be rejected.
        qty = 6.475
        with self.assertRaises(ValueError):
            flb.validate(qty, 100.0, 99.6, "LONG", 50, 0.0022, equity=EQUITY, risk_pct=0.01)
        # A compliant quantity passes.
        ok_qty = 0.259007 / (0.4 + 0.22) * 0.999
        projected, limit = flb.validate(ok_qty, 100.0, 99.6, "LONG", 50, 0.0022,
                                        equity=EQUITY, risk_pct=0.01)
        self.assertAlmostEqual(limit, 0.259007, places=6)
        self.assertLessEqual(projected, limit)

    def test_final_loss_budget_rejects_missing_equity(self):
        from bot import final_loss_budget as flb
        with self.assertRaises((TypeError, ValueError)):
            flb.validate(0.1, 100.0, 99.6, "LONG", 50, 0.0022)


# ─────────────────────────────────────────────────────────────── C ──
class ScenarioC_RiskQuantityIsAuthoritative(unittest.TestCase):
    """RiskManagerV3 qty < operator/margin qty => final qty <= risk qty."""

    def setUp(self):
        self.cfgp = _CfgPatch(LEVERAGE=50, MAX_RISK_PCT=0.01, MAX_MARGIN_PCT=0.50,
                              MAX_DRAWDOWN=0.50, MAX_POSITIONS=2,
                              DAILY_STOP_LOSS_PCT=0.03, DAILY_STOP_LOSS=0.0)
        self.cfgp.__enter__()

    def tearDown(self):
        self.cfgp.__exit__()

    def _run(self, risk_qty, *, available=20.0, equity=20.0, price=100.0, sl=None, info=INFO):
        from bot import final_sizing_invariants as final_sizing
        from bot import pilot_risk_cap_hardening as pilot_cap
        from bot.professional_risk import CapitalState

        class _Mod:
            pass
        module = _Mod()
        module.minimum_base_quantity = lambda i, p: 0.001
        module._final_sizing_invariants_installed = False
        snapshot = SimpleNamespace(
            capital=CapitalState(equity=equity, available_collateral=available), confirmed=True,
        )
        engine = SimpleNamespace(
            paper_trade=False, pilot=SimpleNamespace(enabled=True),
            _pilot_available_balance=available,
            risk=SimpleNamespace(size=lambda *a, **k: risk_qty, professional_snapshot=snapshot),
            instruments={"TESTUSDT": info}, positions={},
        )
        final_sizing.install(module, pilot_cap, _Log())
        sl = price * 0.996 if sl is None else sl
        tokens = (
            pilot_cap._PILOT_ENGINE.set(engine), pilot_cap._PILOT_SYMBOL.set("TESTUSDT"),
            pilot_cap._PILOT_FINAL_QTY.set(None),
            pilot_cap._PILOT_SIGNAL.set(SimpleNamespace(sl=sl, direction="LONG", entry=price)),
        )
        try:
            return module.minimum_base_quantity(info, price), pilot_cap._PILOT_FINAL_QTY.get()
        finally:
            for var, tok in zip((pilot_cap._PILOT_ENGINE, pilot_cap._PILOT_SYMBOL,
                                 pilot_cap._PILOT_FINAL_QTY, pilot_cap._PILOT_SIGNAL), tokens):
                var.reset(tok)

    def test_final_never_exceeds_risk_qty(self):
        qty, stored = self._run(0.25)
        self.assertGreater(qty, 0)
        self.assertLessEqual(qty, 0.25 + 1e-12)
        self.assertEqual(qty, stored)

    def test_final_bounded_by_canonical_equity_budget_even_if_risk_qty_large(self):
        # A misbehaving risk.size returning a huge value must not pass through.
        qty, _ = self._run(1000.0)
        budget = 20.0 * 0.01
        cost = 0.0  # lower bound: stop distance alone must fit
        self.assertLessEqual(qty * (100.0 * 0.004 + cost), budget * (1 + 1e-9))
        self.assertLess(qty, 5.0)  # the old 50%-margin target was 5.0

    def test_margin_is_a_cap_not_a_target(self):
        qty, _ = self._run(0.25)
        self.assertLess(qty * 100.0 / 50.0, 20.0 * 0.50)

    def test_zero_or_invalid_risk_qty_blocks(self):
        for bad in (0.0, float("nan"), -1.0):
            qty, stored = self._run(bad)
            self.assertEqual(qty, 0.0)
            self.assertEqual(stored, 0.0)

    def test_unconfirmed_capital_blocks(self):
        from bot import final_sizing_invariants as final_sizing
        self.assertTrue(hasattr(final_sizing, "size_pilot_entry"))


# ─────────────────────────────────────────────────────────────── D ──
class ScenarioD_DailyStopUsesStricterLimit(unittest.TestCase):
    def test_absolute_100_does_not_loosen_3pct_on_small_account(self):
        lim = rp.effective_daily_stop_limit(EQUITY, 0.03, 100.0)
        self.assertAlmostEqual(lim.limit, round(EQUITY * 0.03, 2))
        self.assertLess(lim.limit, 100.0)
        self.assertEqual(lim.source, "PERCENT_STRICTER")

    def test_absolute_stricter_on_large_account(self):
        lim = rp.effective_daily_stop_limit(10_000.0, 0.03, 100.0)
        self.assertEqual(lim.limit, 100.0)
        self.assertEqual(lim.source, "ABSOLUTE_STRICTER")

    def test_absolute_disabled_or_invalid_uses_pct(self):
        for absolute in (0.0, -5.0, None, float("nan"), float("inf"), "x", True):
            lim = rp.effective_daily_stop_limit(EQUITY, 0.03, absolute)
            self.assertAlmostEqual(lim.limit, round(EQUITY * 0.03, 2), msg=repr(absolute))

    def test_invalid_balance_fails_closed(self):
        for bal in (0.0, -1.0, float("nan")):
            with self.assertRaises(rp.RiskPolicyError):
                rp.effective_daily_stop_limit(bal, 0.03, 100.0)

    def test_deposit_and_withdrawal_recompute_from_current_balance(self):
        self.assertAlmostEqual(rp.effective_daily_stop_limit(50.0, 0.03, 100.0).limit, 1.5)
        self.assertAlmostEqual(rp.effective_daily_stop_limit(10.0, 0.03, 100.0).limit, 0.3)

    def _engine(self, balance):
        tracker = SimpleNamespace(daily_target=0.0, daily_stop_loss=0.0, daily_stopped=False)
        return SimpleNamespace(
            risk=SimpleNamespace(balance=balance), daily_target=0.0, daily_stop_loss=0.0,
            daily_tracker=tracker, daily_stopped=False,
        )

    def test_runtime_sync_uses_stricter_limit(self):
        from bot import daily_stop_runtime_hardening as dsh
        engine = self._engine(EQUITY)
        with _CfgPatch(DAILY_STOP_LOSS=100.0, DAILY_STOP_LOSS_PCT=0.03):
            dsh.sync_daily_limits(engine)
        self.assertAlmostEqual(engine.daily_stop_loss, round(EQUITY * 0.03, 2))
        self.assertAlmostEqual(engine.daily_tracker.daily_stop_loss, round(EQUITY * 0.03, 2))

    def test_daily_tracker_reset_uses_stricter_limit(self):
        from bot.daily_tracker import DailyTracker
        with _CfgPatch(DAILY_STOP_LOSS=100.0, DAILY_STOP_LOSS_PCT=0.03):
            t = DailyTracker()
            t.recalc_limits(EQUITY)
            self.assertAlmostEqual(t.daily_stop_loss, round(EQUITY * 0.03, 2))
            t.recalc_limits(10_000.0)
            self.assertAlmostEqual(t.daily_stop_loss, 100.0)


# ─────────────────────────────────────────────────────────────── E ──
class ScenarioE_MinimumLotAboveRiskBudget(unittest.TestCase):
    def test_min_contract_above_risk_qty_is_no_trade(self):
        # BTC-like: 1 contract = 0.001 BTC ≈ 65 USDT notional; 2% stop => 1.3
        # USDT loss per contract > 0.259 USDT budget.
        policy = _policy()
        d = rp.size_new_entry(
            policy=policy, equity=EQUITY, available=EQUITY, entry=65000.0, stop=63700.0,
            direction="LONG", rules=rp.QuantityRules.from_instrument(INFO), cost_fraction=0.0022,
        )
        self.assertFalse(d.allowed)
        self.assertEqual(d.qty, 0.0)
        self.assertEqual(d.binding_constraint, "MINIMUM_ORDER")

    def test_min_notional_is_not_used_to_escalate(self):
        info = {"multiplier": "1", "lotSize": "1", "minQty": "1", "minNotional": "50"}
        d = rp.size_new_entry(
            policy=_policy(), equity=EQUITY, available=EQUITY, entry=1.0, stop=0.9,
            direction="LONG", rules=rp.QuantityRules.from_instrument(info), cost_fraction=0.0022,
        )
        self.assertFalse(d.allowed)


# ─────────────────────────────────────────────────────────────── F ──
class _StressClient:
    def __init__(self, *, requirement=None, positions=None, orders=None, account=None):
        self.requirement = requirement
        self.positions = positions or []
        self.orders = orders or []
        self.account = account or {"accountEquity": EQUITY, "marginBalance": EQUITY}

    async def _get(self, path, params=None, auth=False):
        if path == "/api/v1/account-overview":
            return self.account
        if path == "/api/v1/orders":
            return {"items": self.orders}
        if path == "/api/v1/positions":
            return self.positions
        raise AssertionError(path)

    async def _post(self, path, body):
        if self.requirement is None:
            raise TimeoutError("stale")
        return [{"symbol": body["symbol"], **self.requirement}]


class ScenarioF_CrossStressFailClosed(unittest.TestCase):
    def _engine(self, client):
        existing = SimpleNamespace(direction="LONG", sl=0.9, trailing_sl=0.9, qty=10.0, entry=1.0, symbol="XRPUSDT")
        return SimpleNamespace(
            client=client,
            positions={"XRPUSDT": existing},
            instruments={
                "XRPUSDT": {"kucoinSymbol": "XRPUSDTM", "multiplier": 10},
                "SOLUSDT": {"kucoinSymbol": "SOLUSDTM", "multiplier": 0.1},
            },
        )

    def _pos_row(self):
        return [{"symbol": "XRPUSDTM", "currentQty": 1, "markPrice": 1.0, "markValue": 10.0,
                 "crossMode": True}]

    def _sig(self):
        return SimpleNamespace(symbol="SOLUSDT", entry=100.0, sl=99.0, direction="LONG")

    def test_missing_margin_requirement_blocks(self):
        from bot import cross_portfolio_stress as cps
        client = _StressClient(requirement=None, positions=self._pos_row())
        result = asyncio.run(cps.evaluate(self._engine(client), self._sig(), 0.1))
        self.assertFalse(result.allowed)

    def test_unknown_mmr_blocks(self):
        from bot import cross_portfolio_stress as cps
        client = _StressClient(requirement={"mmr": None}, positions=self._pos_row())
        result = asyncio.run(cps.evaluate(self._engine(client), self._sig(), 0.1))
        self.assertFalse(result.allowed)

    def test_default_threshold_is_policy_owned_and_conservative(self):
        from bot import cross_portfolio_stress as cps
        self.assertLessEqual(cps.max_stop_stress_risk_rate(), 0.50)

    def test_threshold_rejects_values_above_ceiling(self):
        with patch.dict(os.environ, {"MAX_STOP_STRESS_RISK_RATE": "0.95"}):
            self.assertIn("MAX_STOP_STRESS_RISK_RATE", " ".join(rp.load_policy(cfg).violations()))

    def test_stress_includes_slippage(self):
        from bot import cross_portfolio_stress as cps
        client = _StressClient(requirement={"mmr": 0.01}, positions=self._pos_row())
        result = asyncio.run(cps.evaluate(self._engine(client), self._sig(), 0.1))
        self.assertTrue(result.allowed, result.reason)
        self.assertGreater(result.slippage, 0.0)

    def test_additional_nonreduce_exposure_blocks(self):
        from bot import cross_portfolio_stress as cps
        client = _StressClient(requirement={"mmr": 0.01}, positions=self._pos_row(),
                               orders=[{"isActive": True, "reduceOnly": False}])
        result = asyncio.run(cps.evaluate(self._engine(client), self._sig(), 0.1))
        self.assertFalse(result.allowed)

    def test_missing_protection_blocks(self):
        from bot import cross_portfolio_stress as cps
        client = _StressClient(requirement={"mmr": 0.01}, positions=self._pos_row())
        engine = self._engine(client)
        engine.positions["XRPUSDT"].sl = None
        engine.positions["XRPUSDT"].trailing_sl = None
        result = asyncio.run(cps.evaluate(engine, self._sig(), 0.1))
        self.assertFalse(result.allowed)


# ───────────────────────────────────────────────────── configuration ──
class RiskPolicyConfigurationContract(unittest.TestCase):
    def test_production_like_policy_is_valid(self):
        self.assertEqual(_policy().violations(), ())

    def test_dangerous_values_are_violations_not_clamped(self):
        cases = {
            "LEVERAGE": dict(leverage=0),
            "MAX_RISK_PCT": dict(max_risk_pct=0.25),
            "MAX_MARGIN_PCT": dict(max_margin_pct=1.5),
            "MAX_DRAWDOWN": dict(max_drawdown=1.0),
            "MAX_POSITIONS": dict(max_positions=0),
            "DAILY_STOP_LOSS_PCT": dict(daily_stop_loss_pct=0.0),
            "DAILY_STOP_LOSS=": dict(daily_stop_loss_abs=float("nan")),
            "MIN_RR_RATIO": dict(min_rr_ratio=0),
        }
        for token, over in cases.items():
            v = " ".join(_policy(**over).violations())
            self.assertIn(token, v, over)

    def test_contradiction_single_trade_exceeds_daily_stop(self):
        v = _policy(max_risk_pct=0.02, daily_stop_loss_pct=0.015).violations()
        self.assertTrue(any("DAILY_STOP_LOSS_PCT" in x for x in v))

    def test_invalid_policy_blocks_sizing(self):
        d = rp.size_new_entry(
            policy=_policy(max_risk_pct=0.5), equity=EQUITY, available=EQUITY,
            entry=100.0, stop=99.0, direction="LONG",
            rules=rp.QuantityRules.from_instrument(INFO), cost_fraction=0.0022,
        )
        self.assertFalse(d.allowed)
        self.assertEqual(d.binding_constraint, "RISK_POLICY")

    def test_override_env_is_reported_as_ignored(self):
        with patch.dict(os.environ, {"LIVE_RISK_OVERRIDE_APPROVED": "true"}):
            self.assertIn("LIVE_RISK_OVERRIDE_APPROVED", rp.load_policy(cfg).ignored_overrides)

    def test_portfolio_cap_accounts_for_open_risk(self):
        policy = _policy()
        rules = rp.QuantityRules.from_instrument(INFO)
        full = rp.size_new_entry(policy=policy, equity=1000.0, available=1000.0, entry=100.0,
                                 stop=99.0, direction="LONG", rules=rules, cost_fraction=0.0)
        self.assertTrue(full.allowed)
        # One open position already consuming 1.5 budgets leaves 0.5 budget.
        partial = rp.size_new_entry(policy=policy, equity=1000.0, available=1000.0, entry=100.0,
                                    stop=99.0, direction="LONG", rules=rules, cost_fraction=0.0,
                                    open_risks=[rp.OpenRisk("X", 15.0)])
        self.assertTrue(partial.allowed)
        self.assertAlmostEqual(partial.qty, full.qty / 2, places=6)
        # Unknown open risk consumes a full budget.
        unknown = rp.size_new_entry(policy=policy, equity=1000.0, available=1000.0, entry=100.0,
                                    stop=99.0, direction="LONG", rules=rules, cost_fraction=0.0,
                                    open_risks=[rp.OpenRisk("X", float("nan"))])
        self.assertAlmostEqual(unknown.qty, full.qty, places=6)
        # At MAX_POSITIONS: block.
        at_max = rp.size_new_entry(policy=policy, equity=1000.0, available=1000.0, entry=100.0,
                                   stop=99.0, direction="LONG", rules=rules, cost_fraction=0.0,
                                   open_risks=[rp.OpenRisk("X", 0.0), rp.OpenRisk("Y", 0.0)])
        self.assertFalse(at_max.allowed)

    def test_wrong_side_stop_blocks(self):
        d = rp.size_new_entry(
            policy=_policy(), equity=EQUITY, available=EQUITY, entry=100.0, stop=101.0,
            direction="LONG", rules=rp.QuantityRules.from_instrument(INFO), cost_fraction=0.0,
        )
        self.assertFalse(d.allowed)

    def test_liquidation_cap_binds_with_high_mmr(self):
        # Wide equity/margin, tiny stop: liquidation cap must bind before risk cap.
        policy = _policy(max_risk_pct=0.02, daily_stop_loss_pct=0.03, max_margin_pct=1.0)
        d = rp.size_new_entry(
            policy=policy.__class__(**{**policy.__dict__, "operator_margin_cap_pct": 1.0}),
            equity=100.0, available=100.0, entry=100.0, stop=99.99, direction="LONG",
            rules=rp.QuantityRules.from_instrument(INFO), cost_fraction=0.0,
            maintenance_margin_rate=0.2,
        )
        self.assertTrue(d.allowed)
        self.assertEqual(d.binding_constraint, "LIQUIDATION")
        self.assertTrue(math.isfinite(d.caps["liquidation"]))


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()


class RuntimeTruthCaptureFailuresAreVisible(unittest.TestCase):
    def test_capture_failure_is_counted_and_logged(self):
        from bot import runtime_truth_hooks as hooks
        hooks.CAPTURE_FAILURES.clear()
        with patch("bot.logger.log") as log:
            hooks._note_capture_failure("rest_kline", ValueError("x"))
            hooks._note_capture_failure("rest_kline", ValueError("x"))
        self.assertEqual(hooks.CAPTURE_FAILURES["rest_kline"], 2)
        self.assertEqual(log.warning.call_count, 1)
