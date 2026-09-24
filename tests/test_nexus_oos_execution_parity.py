"""Execution-parity replay: event engine, exit emulation, manifest, sizing parity.

Offline and deterministic. Covers P0-PARITY-01..07 and P1-PARITY-08 /
P1-PORTFOLIO-10..12 acceptance tests.
"""
import copy
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot import nexus_oos_execution_parity as xp
from bot import nexus_oos_portfolio_engine as pe
from bot import nexus_oos_replay_manifest as rm

ROOT = Path(__file__).resolve().parent.parent
D0 = 1_760_054_400_000          # 2025-10-10 00:00:00 UTC
M15 = 15 * 60 * 1000
H = 4 * M15


def _manifest(**over):
    raw = json.loads(rm.DEFAULT_PATH.read_text(encoding="utf-8"))
    for k, v in over.items():
        raw["values"][k]["value"] = v
    return rm.ReplayManifest(raw, source="test")


INFO = {"minQty": 1.0, "lotSize": 1.0, "qtyStep": 1.0, "multiplier": 0.01, "minNotional": 0.0,
        "tickSize": 0.01, "contractMaintainMarginReference": 0.004}
SYMS = ("BTCUSDT", "XRPUSDT", "LINKUSDT", "NEARUSDT", "ATOMUSDT", "SOLUSDT")
INSTR = {s: dict(INFO) for s in SYMS}
MMR = {s: 0.004 for s in SYMS}


def _row(ts, symbol, legs, marks, *, direction="LONG", entry=100.0, sl=99.0, tp=103.0,
         funding=(), score=70, rr=3.0):
    return {"ts": ts, "symbol": symbol, "direction": direction, "approved": True,
            "executable": True, "strategy_score": score, "score_adjusted": score, "rr": rr,
            "signal_entry": entry, "sl": sl, "tp": tp, "fill": entry, "legs": legs,
            "marks": marks, "funding": list(funding), "fee_rate": 0.0006,
            "cost_fraction": 0.0022, "month": "2025-10"}


def _later(ts, symbol, hours=1):
    end = ts + hours * H
    return _row(ts, symbol, [(end, 1.0, 100.2, "NATIVE_TP")],
                [(ts + k * M15, 100.1) for k in range(1, hours * 4)])


def _run(rows, manifest=None, **kw):
    return pe.run_portfolio(rows, manifest or _manifest(), instruments=INSTR, mmr_proxy=MMR, **kw)


# ── P0-PARITY-01: daily stop anchored at UTC midnight ───────────────────────
class DailyStopMidnightAnchor(unittest.TestCase):
    def _post_midnight_loss(self):
        a0 = D0 - H                                   # 23:00 previous UTC day
        marks = [(D0 - 3 * M15, 100.0), (D0 - 2 * M15, 99.9), (D0 - M15, 99.8), (D0, 99.6),
                 (D0 + M15, 97.0)]
        a = _row(a0, "BTCUSDT", [(D0 + 2 * M15, 1.0, 94.0, "NATIVE_SL")], marks)   # closes 00:30
        b = _later(D0 + 3 * H, "XRPUSDT")                                          # 03:00
        return [a, b]

    def test_loss_after_midnight_counts_against_new_day_production_semantics(self):
        out = _run(self._post_midnight_loss())
        self.assertEqual(out["blocked_daily_stop"], 1)
        self.assertEqual(out["daily_stop_days"], 1)
        self.assertEqual(out["total_trades"], 1)
        self.assertEqual(out["daily_pnl_semantics"], "PRODUCTION_REALIZED_TODAY_PLUS_OPEN_UNREALIZED")

    def test_loss_after_midnight_counts_against_new_day_equity_anchored(self):
        out = _run(self._post_midnight_loss(), daily_pnl_semantics="EQUITY_ANCHORED_AT_UTC_MIDNIGHT")
        # Only the 00:00 -> 00:30 move (99.6 -> 94) is today's; it breaches 3% of equity.
        self.assertEqual(out["blocked_daily_stop"], 1)

    def test_reset_happens_at_midnight_not_at_first_candidate(self):
        # Old-day stop at 23:30 blocks 23:45; the 00:15 candidate is admitted.
        a0 = D0 - 2 * H
        a = _row(a0, "BTCUSDT", [(D0 - 2 * M15, 1.0, 94.0, "NATIVE_SL")],
                 [(a0 + k * M15, 99.8) for k in range(1, 6)])
        blocked = _later(D0 - M15, "XRPUSDT")
        admitted = _later(D0 + M15, "LINKUSDT")
        out = _run([a, blocked, admitted])
        self.assertEqual(out["blocked_daily_stop"], 1)
        self.assertEqual(out["total_trades"], 2)
        self.assertIn("LINKUSDT", out["by_symbol"])

    def test_semantics_differ_on_carried_unrealized_loss(self):
        a0 = D0 - H
        marks = [(D0 - 3 * M15, 95.0), (D0 - 2 * M15, 95.0), (D0 - M15, 95.0), (D0, 95.2),
                 (D0 + M15, 95.5)]
        a = _row(a0, "BTCUSDT", [(D0 + 2 * M15, 1.0, 96.0, "NATIVE_SL")], marks)
        b = _later(D0 + 3 * H, "XRPUSDT")
        prod = _run([a, b])
        anchored = _run([a, b], daily_pnl_semantics="EQUITY_ANCHORED_AT_UTC_MIDNIGHT")
        # Production counts the carried unrealized loss again after the reset.
        self.assertEqual(prod["blocked_daily_stop"], 1)
        self.assertEqual(anchored["blocked_daily_stop"], 0)
        self.assertEqual(anchored["total_trades"], 2)


# ── P0-PARITY-02: bar-by-bar HWM / drawdown ─────────────────────────────────
class BarByBarDrawdown(unittest.TestCase):
    def test_intermediate_peak_is_seen_by_the_drawdown_gate(self):
        m = _manifest(MAX_DRAWDOWN=0.02)
        marks = [(D0 + M15, 101.0), (D0 + 2 * M15, 104.0), (D0 + 3 * M15, 102.0),
                 (D0 + 4 * M15, 100.5), (D0 + 5 * M15, 100.5), (D0 + 6 * M15, 100.5),
                 (D0 + 7 * M15, 100.5)]
        a = _row(D0, "BTCUSDT", [(D0 + 2 * H, 1.0, 100.5, "NATIVE_SL")], marks)
        b = _later(D0 + H, "XRPUSDT")
        toggles = pe.Toggles(single_position_liquidation_rule=False)
        out = _run([a, b], m, toggles=toggles)
        self.assertEqual(out["blocked_drawdown"], 1)
        self.assertGreater(out["portfolio_max_drawdown"], 0.02)
        self.assertGreater(out["drawdown_gate_active_bars"], 0)

    def test_equity_curve_has_every_bar(self):
        a = _row(D0, "BTCUSDT", [(D0 + 2 * H, 1.0, 100.5, "NATIVE_TP")],
                 [(D0 + k * M15, 100.2) for k in range(1, 8)])
        out = _run([a])
        self.assertEqual(out["accounting_invariants"], "PASS")
        self.assertGreaterEqual(out["accounting_invariant_checks"], 8)


# ── P0-PARITY-03: intrabar exit and same-timestamp precedence ───────────────
class SameTimestampExitEntry(unittest.TestCase):
    def _rows(self):
        a = _row(D0, "BTCUSDT", [(D0 + H, 1.0, 99.0, "NATIVE_SL")],
                 [(D0 + k * M15, 99.5) for k in range(1, 4)])
        b = _later(D0 + H, "BTCUSDT")
        return [a, b]

    def test_exit_at_t_releases_position_before_candidates_at_t(self):
        out = _run(self._rows(), toggles=pe.Toggles(cooldown_after_close=False))
        self.assertEqual(out["total_trades"], 2)
        self.assertEqual(out["skipped"], {})

    def test_production_cooldown_blocks_same_symbol_reentry(self):
        out = _run(self._rows())
        self.assertEqual(out["skipped"].get("cooldown_or_circuit_breaker"), 1)

    def test_single_position_rule_uses_state_after_exits(self):
        rows = self._rows()
        rows[1]["symbol"] = "XRPUSDT"
        out = _run(rows)                    # A closed at T -> no open position at T
        self.assertEqual(out["total_trades"], 2)

    def test_single_position_rule_blocks_a_second_concurrent_entry(self):
        a = _row(D0, "BTCUSDT", [(D0 + 2 * H, 1.0, 100.0, "NATIVE_TP")],
                 [(D0 + k * M15, 100.0) for k in range(1, 8)])
        out = _run([a, _later(D0 + H, "XRPUSDT")])
        self.assertEqual(out["skipped"].get("liquidation_guard_multi_position"), 1)
        self.assertEqual(out["max_concurrent_positions"], 1)

    def test_attribution_toggle_really_disables_single_position_rule(self):
        a = _row(D0, "BTCUSDT", [(D0 + 2 * H, 1.0, 100.0, "NATIVE_TP")],
                 [(D0 + k * M15, 100.0) for k in range(1, 8)])
        out = _run([a, _later(D0 + H, "XRPUSDT")],
                   toggles=pe.Toggles(single_position_liquidation_rule=False))
        self.assertEqual(out["total_trades"], 2)
        self.assertEqual(out["max_concurrent_positions"], 2)

    def test_production_rank_order_at_same_timestamp(self):
        c = _later(D0, "XRPUSDT")
        d = _later(D0, "LINKUSDT")
        d["score_adjusted"] = d["strategy_score"] = 80
        out = _run([c, d])
        self.assertEqual(set(out["by_symbol"]), {"LINKUSDT"})

    def test_off_grid_event_fails_closed(self):
        a = _row(D0, "BTCUSDT", [(D0 + H + 1, 1.0, 100.0, "NATIVE_TP")], [])
        with self.assertRaises(pe.AccountingInvariantError):
            _run([a])


# ── P1-PORTFOLIO-11/12: accounting identities and funding timing ────────────
class AccountingAndFunding(unittest.TestCase):
    def test_partial_exit_funding_and_ledger(self):
        f_full, f_half, f_after = D0 + 2 * M15, D0 + 6 * M15, D0 + 3 * H
        a = _row(D0, "BTCUSDT",
                 [(D0 + H, 0.5, 101.03, "PARTIAL_TP1"), (D0 + 2 * H, 0.5, 100.0, "BREAK_EVEN_SL")],
                 [(D0 + k * M15, 100.5) for k in range(1, 8)],
                 funding=[(f_full, 0.0001, 100.0), (f_half, 0.0001, 100.0), (f_after, 0.0001, 100.0)])
        out = _run([a])
        t = out["by_symbol"]["BTCUSDT"]
        self.assertEqual(out["total_trades"], 1)
        # Re-derive the quantity from the ledger: LONG pays positive funding.
        fees = out["total_fees"]
        funding = out["total_funding"]
        gross = t["net_pnl"] - funding + fees
        q = gross / (0.5 * 1.03 + 0.5 * 0.0)
        self.assertAlmostEqual(funding, -(0.0001 * 100.0 * q + 0.0001 * 100.0 * q / 2), places=9)
        self.assertAlmostEqual(out["ending_equity"], 1000.0 + t["net_pnl"], places=9)
        self.assertEqual(out["accounting_invariants"], "PASS")

    def test_no_funding_after_close_and_margin_released_once(self):
        a = _row(D0, "BTCUSDT", [(D0 + H, 1.0, 100.0, "NATIVE_TP")],
                 [(D0 + k * M15, 100.0) for k in range(1, 4)],
                 funding=[(D0 + 2 * H, 0.01, 100.0)])
        out = _run([a, _later(D0 + 2 * H, "XRPUSDT")])
        self.assertEqual(out["total_funding"], 0.0)
        self.assertEqual(out["total_trades"], 2)


# ── P1-PORTFOLIO-10: robustness semantics ───────────────────────────────────
class PathBootstrap(unittest.TestCase):
    def _rows(self):
        rows = []
        for d in range(12):
            ts = D0 + d * 24 * H + 2 * H
            px = 101.0 if d % 3 else 99.0
            rows.append(_row(ts, SYMS[d % len(SYMS)], [(ts + H, 1.0, px, "NATIVE_TP")],
                             [(ts + k * M15, 100.0) for k in range(1, 4)]))
        return rows

    def test_path_bootstrap_reruns_state_machine_deterministically(self):
        a = pe.path_bootstrap(self._rows(), _manifest(), instruments=INSTR, mmr_proxy=MMR,
                              replicates=100, seed=3)
        b = pe.path_bootstrap(self._rows(), _manifest(), instruments=INSTR, mmr_proxy=MMR,
                              replicates=100, seed=3)
        self.assertEqual(a, b)
        self.assertEqual(a["replicates"], 100)
        self.assertEqual(a["method"], "UTC_BLOCK_RESAMPLED_CANDIDATE_TIMELINE_RERUN_THROUGH_STATE_MACHINE")
        lo, hi = a["net_return_ci"]
        self.assertLessEqual(lo, hi)
        self.assertEqual(a["status"], "APPROXIMATE_NON_AUTHORITATIVE")
        self.assertEqual(a["authority"], "NONE")

    def test_trade_level_ci_is_labelled_non_authoritative(self):
        out = _run(self._rows())
        self.assertEqual(out["approximate_trade_level_ci"]["authority"], "NONE")

    def test_contract_spec_sensitivity(self):
        cs = pe.contract_spec_sensitivity(self._rows(), _manifest(), instruments=INSTR, mmr_proxy=MMR)
        for k in ("current", "lot_size_x10", "multiplier_x10", "min_notional_10usdt", "mmr_x2"):
            self.assertIn(k, cs)
        self.assertEqual(cs["contract_metadata"], "CURRENT_CONTRACT_SPEC_PROXY")
        self.assertFalse(cs["historical_specs_available"])


# ── P0-PARITY-07: production exit emulation ─────────────────────────────────
def _bars(ohlc, start=D0):
    return [{"ts": start + i * M15, "o": o, "h": h, "l": l, "c": c} for i, (o, h, l, c) in enumerate(ohlc)]


def _sim(bars, **kw):
    args = dict(direction="LONG", bars=bars, start_idx=0, signal_entry=100.0, signal_sl=99.0,
                signal_tp=103.0, slippage_rate=0.0)
    args.update(kw)
    return xp.simulate_production_exit(**args)


class ProductionExitEmulation(unittest.TestCase):
    def test_native_stop_is_not_shifted_by_the_fill(self):
        bars = _bars([(100.5, 100.6, 99.2, 99.4), (99.4, 99.5, 98.9, 99.0)])
        s = _sim(bars)
        self.assertEqual(s["legs"][0][3], "NATIVE_SL")
        self.assertEqual(s["legs"][0][0], D0 + 2 * M15)       # not in bar 0 (99.2 > 99.0)
        legacy = _sim(bars, policy=xp.ExitPolicy(shift_native_stops=True))
        self.assertEqual(legacy["legs"][0][0], D0 + M15)       # shifted stop 99.5 hit in bar 0

    def test_partial_at_1r_plus_buffer_then_break_even(self):
        bars = _bars([(100.0, 101.1, 99.9, 101.0), (101.0, 101.2, 99.95, 100.2)])
        s = _sim(bars)
        kinds = [l[3] for l in s["legs"]]
        self.assertEqual(kinds, ["PARTIAL_TP1", "BREAK_EVEN_SL"])
        self.assertAlmostEqual(s["legs"][0][2], 101.0 + 100.0 * 0.0003)
        self.assertAlmostEqual(s["legs"][0][1], 0.5)
        self.assertAlmostEqual(s["legs"][1][2], 100.0)

    def test_same_bar_break_even_only_on_certain_crossing(self):
        s = _sim(_bars([(100.0, 101.1, 99.5, 99.8)]))
        self.assertEqual([l[3] for l in s["legs"]], ["PARTIAL_TP1", "BREAK_EVEN_SL_SAME_BAR"])

    def test_stop_first_on_ambiguous_bar(self):
        s = _sim(_bars([(100.0, 103.5, 98.5, 101.0)]))
        self.assertEqual([l[3] for l in s["legs"]], ["NATIVE_SL"])

    def test_gap_through_stop_fills_at_open(self):
        s = _sim(_bars([(100.0, 100.2, 99.8, 100.0), (97.0, 97.5, 96.5, 97.0)]))
        self.assertAlmostEqual(s["legs"][0][2], 97.0)

    def test_native_tp_after_partial(self):
        s = _sim(_bars([(100.0, 103.2, 99.9, 103.0)]))
        self.assertEqual([l[3] for l in s["legs"]], ["PARTIAL_TP1", "NATIVE_TP"])
        self.assertAlmostEqual(s["legs"][1][2], 103.0)

    def test_rr_double_before_partial_when_partial_disabled(self):
        s = _sim(_bars([(100.0, 102.1, 99.9, 102.0)]), policy=xp.ExitPolicy(enable_partial=False))
        self.assertEqual([l[3] for l in s["legs"]], ["RR_DOUBLE"])
        self.assertAlmostEqual(s["legs"][0][2], 102.0)

    def test_no_time_exit_in_production(self):
        flat = [(100.2, 100.4, 99.95, 100.2)] * 120
        s = _sim(_bars(flat + [(100.2, 100.3, 98.5, 98.6)]), signal_tp=110.0)
        self.assertEqual(s["exit_reason"], "NATIVE_SL")
        self.assertEqual(s["exit_ts"], D0 + 121 * M15)

    def test_trailing_uses_unrescaled_peak_after_partial_and_side_check(self):
        # Partial at 101.03; peak_pnl recorded with full qty; trailing new_sl =
        # entry + (peak/qty_half) * 0.75 = 100 + 2*MFE*0.75 is above the mark ->
        # rejected (stop unchanged at break-even).
        bars = _bars([(100.0, 101.1, 99.95, 101.0), (101.0, 101.4, 100.9, 101.3)])
        s = _sim(bars, signal_tp=102.0, policy=xp.ExitPolicy(trailing_trigger=0.5))
        self.assertEqual(s["outcome_status"], "RIGHT_CENSORED_DATA_END")
        self.assertIsNone(s["exit_reason"])
        self.assertEqual([l[3] for l in s["legs"]], ["PARTIAL_TP1"])   # no artificial close
        # With tp=103 the trigger is reachable: bar 1 high 102.2 gives peak
        # max(1.03 (full qty), 1.1 (half qty)) = 1.1 -> excursion 2.2 ->
        # stop 100 + 2.2 * 0.75 = 101.65 (valid below the mark); bar 2 hits it.
        bars2 = _bars([(100.0, 101.1, 99.95, 101.0), (101.0, 102.2, 100.9, 102.0),
                       (102.0, 102.1, 101.5, 101.6)])
        s2 = _sim(bars2, signal_tp=103.0)
        self.assertEqual([l[3] for l in s2["legs"]], ["PARTIAL_TP1", "TRAILING_SL"])
        self.assertAlmostEqual(s2["legs"][1][2], 101.65)
        self.assertEqual(s2["exit_ts"], D0 + 3 * M15)

    def test_censoring_at_data_end_is_reported(self):
        s = _sim(_bars([(100.0, 100.3, 99.9, 100.1)] * 3), signal_tp=110.0)
        self.assertEqual(s["censored"], "RIGHT_CENSORED_DATA_END")
        self.assertEqual(s["legs"], [])                 # no manufactured close
        self.assertIsNone(s["exit_ts"])
        self.assertEqual(s["open_qty_at_censor"], 1.0)

    def test_short_mirror(self):
        s = _sim(_bars([(100.0, 100.1, 98.9, 99.0), (99.0, 100.05, 98.95, 100.02)]),
                 direction="SHORT", signal_sl=101.0, signal_tp=97.0)
        self.assertEqual([l[3] for l in s["legs"]], ["PARTIAL_TP1", "BREAK_EVEN_SL"])


# ── production facts the parity model relies on ─────────────────────────────
class ProductionFactsGuard(unittest.TestCase):
    def test_tp1_equals_tp2_in_production_signals(self):
        from bot.strategy import Signal
        sig = Signal("BTCUSDT", "LONG", 100.0, 99.0, 103.0, 0.7, score=70)
        self.assertEqual((sig.tp1, sig.tp2), (sig.tp, sig.tp))
        callers = [p for p in (ROOT / "bot").glob("*.py")
                   if re.search(r"\bcalc_sl_tp\(", p.read_text(encoding="utf-8"))
                   and p.name != "strategy.py"]
        strategy_calls = re.findall(r"\bcalc_sl_tp\(", (ROOT / "bot" / "strategy.py").read_text(encoding="utf-8"))
        self.assertEqual(callers, [])
        self.assertEqual(len(strategy_calls), 1)   # the definition only

    def test_live_discretionary_exits_disabled(self):
        src = (ROOT / "bot" / "operator_loss_policy.py").read_text(encoding="utf-8")
        self.assertIn("TradingEngine._check_stagnation_and_invalidation = no_discretionary_loss_exit", src)
        overlays = (ROOT / "bot" / "runtime_overlays.py").read_text(encoding="utf-8")
        self.assertLess(overlays.index("exit_policy_telemetry.install"),
                        overlays.index("operator_loss_policy.install"))

    def test_second_concurrent_position_is_never_liquidation_effective(self):
        from bot import liquidation
        a = liquidation.analyze(entry=100.0, stop=99.5, leverage=50, is_long=True,
                                symbol="BTCUSDT", n_open_positions=2)
        self.assertFalse(a.stop_effective)
        guard = (ROOT / "bot" / "liquidation_override_guard.py").read_text(encoding="utf-8")
        self.assertIn('raw == "true"', guard)
        engine = (ROOT / "bot" / "engine.py").read_text(encoding="utf-8")
        self.assertIn("n_open_positions=len(self.positions) + 1", engine)

    def test_partial_tp_formula_in_production(self):
        engine = (ROOT / "bot" / "engine.py").read_text(encoding="utf-8")
        self.assertIn("funding_cost = pos.entry * 0.0001 * 3", engine)
        hard = (ROOT / "bot" / "partial_tp_execution_hardening.py").read_text(encoding="utf-8")
        self.assertIn("funding_cost = pos.entry * 0.0001 * 3", hard)

    def test_native_tpsl_uses_signal_levels_before_fill_shift(self):
        engine = (ROOT / "bot" / "engine.py").read_text(encoding="utf-8")
        place = engine.index("sl=sig.sl, tp=sig.tp,\n                        instruments=self.instruments,\n                        idem_key=_idem")
        shift = engine.index("sig.sl    += _delta")
        self.assertLess(place, shift)

    def test_session_boundaries_match_engine(self):
        from datetime import datetime, timezone
        from bot import engine as eng
        for hour in range(24):
            fixed = datetime(2025, 10, 10, hour, 5, tzinfo=timezone.utc)

            class _DT(datetime):
                @classmethod
                def now(cls, tz=None):
                    return fixed

            with patch.object(eng, "datetime", _DT):
                self.assertEqual(eng.TradingEngine._get_market_session(), xp.market_session(hour))

    def test_funnel_flags(self):
        from bot.engine import TradingEngine
        sig = SimpleNamespace(symbol="SOLUSDT", direction="SHORT", score=66, regime="TRENDING_UP",
                              expected_pnl=0.4)
        f = xp.funnel_flags(sig, D0 + 2 * H, TradingEngine)     # 02:00 UTC -> ASIA
        self.assertEqual(f["session"], "ASIA")
        self.assertEqual(f["adjusted_score"], 58)
        self.assertFalse(f["regime_allows_direction"])
        self.assertTrue(f["expected_pnl_positive"])

    def test_drift_gate(self):
        self.assertTrue(xp.drift_gate(100.0, 100.3, "LONG", 20)["blocked"])
        self.assertFalse(xp.drift_gate(100.0, 99.7, "LONG", 20)["blocked"])   # favourable
        self.assertFalse(xp.drift_gate(100.0, 100.1, "LONG", 20)["blocked"])

    def test_geometry_uses_production_function_and_restores_registry(self):
        from bot import liquidation
        from bot.kucoin_contract_risk_hardening import _geometry_from_exact_mmr
        sig = SimpleNamespace(symbol="BTCUSDT", direction="LONG", entry=100.0, sl=98.6, tp=103.0,
                              total_fees=0.2)
        before = dict(liquidation._MMR_BY_SYMBOL)
        g = xp.production_geometry(sig, leverage=50, mmr=0.004, fee_multiplier=2.0)
        self.assertEqual(dict(liquidation._MMR_BY_SYMBOL), before)
        self.assertEqual(g["status"], "ADJUSTED")
        liquidation.set_mmr_from_api("BTCUSDT", 0.004, source="t")
        try:
            direct = _geometry_from_exact_mmr(liquidation, sig, 50)
        finally:
            liquidation._MMR_BY_SYMBOL.pop("BTCUSDT", None)
            liquidation._MMR_SOURCE.pop("BTCUSDT", None)
        self.assertAlmostEqual(g["sl"], round(direct["sl"], 8))
        self.assertAlmostEqual(g["tp"], round(direct["tp"], 8))
        safe = xp.production_geometry(SimpleNamespace(**{**sig.__dict__, "sl": 99.5}),
                                      leverage=50, mmr=0.004, fee_multiplier=2.0)
        self.assertEqual(safe["status"], "SAFE")
        blocked = xp.production_geometry(SimpleNamespace(**{**sig.__dict__, "sl": 95.0, "tp": 110.0}),
                                         leverage=50, mmr=0.004, fee_multiplier=2.0)
        self.assertEqual(blocked["status"], "BLOCK")


class ParityMatrices(unittest.TestCase):
    def test_required_exit_rules_classified(self):
        rules = {r["rule"] for r in xp.EXIT_PARITY_MATRIX}
        for rule in ("native_hard_sl", "native_tp", "tp1_partial_50pct", "tp2", "break_even_after_tp1",
                     "trailing_stop", "rr_double_2R_exit", "min_hold_90m", "stagnation_4h",
                     "choch_invalidation", "regime_invalidation", "signal_invalidation",
                     "restart_durable_state", "time_exit", "funding_settlement"):
            self.assertIn(rule, rules)
        vocab = {xp.FULLY_REPLAYED, xp.APPROXIMATED, xp.NOT_REPLAYABLE, xp.LIVE_ONLY,
                 xp.INACTIVE_IN_LIVE, xp.TELEMETRY_ONLY, xp.NOT_AUTHORITATIVE_IN_LIVE, xp.UNREACHABLE}
        for row in xp.EXIT_PARITY_MATRIX + xp.PRETRADE_GATE_MATRIX:
            self.assertIn(row["classification"], vocab)

    def test_parity_is_incomplete_and_names_blockers(self):
        ex, pre = xp.exit_parity_status(), xp.pretrade_parity_status()
        self.assertFalse(ex["complete"])
        self.assertFalse(pre["complete"])
        self.assertIn("trailing_stop", ex["blocking_rules"])
        self.assertIn("pre_dispatch spread / depth", pre["blocking_rules"])
        self.assertIn("pilot_session_submission_cap (2 per process session)", pre["blocking_rules"])


# ── P0-PARITY-04: pinned manifest ───────────────────────────────────────────
class ReplayPolicyManifest(unittest.TestCase):
    def test_default_manifest_loads_and_matches_runtime(self):
        m = rm.load()
        self.assertEqual(len(m.sha256), 64)
        self.assertEqual(m.verify_runtime()["mismatches"], {})
        rep = m.report()
        self.assertEqual(rep["values"]["LEVERAGE"], 50)
        self.assertIn("LEVERAGE", rep["differs_from_code_default"])
        self.assertFalse(rep["secrets_read"])
        self.assertEqual(rep["policy_sha256"], m.sha256)

    def test_missing_extra_and_bad_types_fail_closed(self):
        raw = json.loads(rm.DEFAULT_PATH.read_text(encoding="utf-8"))
        broken = copy.deepcopy(raw)
        del broken["values"]["MAX_RISK_PCT"]
        with self.assertRaisesRegex(rm.ManifestError, "MISSING"):
            rm.ReplayManifest(broken, source="t")
        extra = copy.deepcopy(raw)
        extra["values"]["API_KEY"] = {"value": "x"}
        with self.assertRaisesRegex(rm.ManifestError, "UNKNOWN"):
            rm.ReplayManifest(extra, source="t")
        bad = copy.deepcopy(raw)
        bad["values"]["MAX_POSITIONS"]["value"] = "2"
        with self.assertRaisesRegex(rm.ManifestError, "BAD_TYPE"):
            rm.ReplayManifest(bad, source="t")
        invalid = copy.deepcopy(raw)
        invalid["values"]["MAX_RISK_PCT"]["value"] = 0.5
        with self.assertRaisesRegex(rm.ManifestError, "RISK_POLICY_INVALID"):
            rm.ReplayManifest(invalid, source="t")

    def test_runtime_mismatch_fails_closed(self):
        from bot.config import cfg
        m = rm.load()
        with patch.object(cfg, "MIN_ENTRY_SCORE", 61):
            with self.assertRaisesRegex(rm.ManifestError, "MANIFEST_RUNTIME_MISMATCH"):
                m.verify_runtime()

    def test_sha256_changes_with_values(self):
        a = _manifest()
        b = _manifest(MAX_POSITIONS=3)
        self.assertNotEqual(a.sha256, b.sha256)
        self.assertEqual(a.sha256, _manifest().sha256)

    def test_replay_cli_exits_2_on_missing_manifest(self):
        from bot import nexus_oos_real_replay as replay
        code = replay.main(["--symbols", "BTCUSDT", "--policy-manifest", "/nonexistent.json",
                            "--output", "/tmp/should_not_exist.json"])
        self.assertEqual(code, 2)
        self.assertFalse(Path("/tmp/should_not_exist.json").exists())


# ── P0-PARITY-05: sizing parity with production final sizing ────────────────
class SizingParity(unittest.TestCase):
    def _production_qty(self, *, equity, available, entry, sl, direction, info, symbol="BTCUSDT"):
        from bot.config import cfg
        from bot import final_sizing_invariants as fsi
        from bot.professional_risk import CapitalState
        from bot.professional_risk_adapter import ProfessionalRiskAdapter
        from bot.logger import log
        legacy = SimpleNamespace(balance=available, balance_confirmed=True, _ready=True,
                                 update=lambda b: None)
        adapter = ProfessionalRiskAdapter(legacy)
        adapter.set_plan(symbol=symbol, entry=entry, stop=sl, risk_pct=0.01)
        adapter.update_capital(CapitalState(equity=equity, available_collateral=available))
        engine = SimpleNamespace(risk=adapter, _pilot_available_balance=available,
                                 instruments={symbol: info}, positions={},
                                 _effective_risk_pct=lambda: 0.01)
        sig = SimpleNamespace(direction=direction, sl=sl, entry=entry)
        with patch.object(cfg, "LEVERAGE", 50), patch.object(cfg, "MAX_DRAWDOWN", 0.50), \
                patch.object(cfg, "DAILY_STOP_LOSS", 100.0):
            qty, _ = fsi.size_pilot_entry(engine, symbol, info, entry, sig, log)
        return qty

    def _replay_qty(self, *, equity, available, entry, sl, direction, info, symbol="BTCUSDT"):
        from bot.kucoin_execution_model import estimated_round_trip_cost_pct
        m = rm.load()
        cost = xp.production_cost_fraction(
            taker_fee=m["TAKER_FEE"], expected_slippage_pct=m["NEXUS_EXPECTED_SLIPPAGE_PCT"],
            modeled_round_trip_pct=estimated_round_trip_cost_pct(symbol, m["TAKER_FEE"]))
        out = xp.replay_size_entry(policy=m.risk_policy(), info=info, equity=equity,
                                   available=available, signal_entry=entry, signal_sl=sl,
                                   direction=direction, cost_fraction=cost, risk_pct=0.01,
                                   max_adverse_entry_drift=m["NEXUS_MAX_SIGNAL_DRIFT_BPS"] / 1e4)
        return out["qty"]

    def test_identical_snapshots_give_identical_quantities(self):
        cases = [
            dict(equity=1000.0, available=1000.0, entry=100.0, sl=99.0, direction="LONG"),
            dict(equity=25.9007, available=25.9007, entry=150.0, sl=149.4, direction="LONG"),
            dict(equity=500.0, available=300.0, entry=2.5, sl=2.53, direction="SHORT"),
            dict(equity=1000.0, available=50.0, entry=60000.0, sl=59400.0, direction="LONG"),
            dict(equity=12.0, available=12.0, entry=60000.0, sl=59900.0, direction="LONG"),
        ]
        infos = [dict(INFO), {**INFO, "multiplier": 0.001}, {**INFO, "multiplier": 10.0},
                 {**INFO, "minNotional": 5.0}]
        checked = 0
        for c in cases:
            for info in infos:
                with self.subTest(case=c, info=info):
                    self.assertEqual(self._production_qty(info=info, **c),
                                     self._replay_qty(info=info, **c))
                    checked += 1
        self.assertEqual(checked, 20)

    def test_drift_allowance_matters_so_it_must_be_supplied(self):
        from bot import risk_policy as rp
        m = rm.load()
        base = dict(policy=m.risk_policy(), equity=1000.0, available=1000.0, entry=100.0, stop=99.8,
                    direction="LONG", rules=rp.QuantityRules.from_instrument(INFO),
                    cost_fraction=0.0022, risk_pct=0.01, maintenance_margin_rate=0.004)
        with_drift = rp.size_new_entry(**base, max_adverse_entry_drift=0.002).qty
        without = rp.size_new_entry(**base, max_adverse_entry_drift=0.0).qty
        self.assertLess(with_drift, without)


if __name__ == "__main__":
    unittest.main()
