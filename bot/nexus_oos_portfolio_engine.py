"""PORTFOLIO_EXECUTION_REPLAY v2: bar-by-bar event engine with production parity.

Research only: no network, no exchange mutation, no runtime mutation.

Input rows are NEXUS-approved, production-executable candidates produced by
the candidate replay. Each row carries its production-parity trade path from
``nexus_oos_execution_parity.simulate_production_exit``: entry fill, exit legs
(timestamp, fraction, fill price, reason), 15m closing marks and the public
funding events that fall inside its holding period.

Timeline. The engine walks EVERY closed 15m bar from the first decision to the
last exit. Same-timestamp precedence at bar close T (mirrors one production
tick: exits/position management, then account/risk refresh, then the scan):

  1. FUNDING      settlements with timepoint in (T-15m, T] on the quantity open
                  at that time (before any leg at T, as in the candidate model);
  2. EXITS        legs whose modeled exit is in the bar ending at T: PnL and
                  fees realized once at T, margin released pro rata once,
                  position unavailable after T;
  3. MARK         remaining positions marked at the close of the bar ending T;
  4. DAILY_RESET  when T is 00:00 UTC (explicit event; not the first candidate);
  5. ACCOUNT      equity, research HWM, drawdown, daily PnL, circuit breakers,
                  equity curve;
  6. CANDIDATES   decisions at T (candles closed at T), ordered by the
                  production rank (score * rr desc, then symbol), each seeing
                  the state produced by 1-5 and by earlier entries at T;
  7. ENTRY        market fill at the open of the bar starting at T; entry fee
                  booked at T.

Daily PnL semantics (manifest DAILY_PNL_SEMANTICS):
  PRODUCTION_REALIZED_TODAY_PLUS_OPEN_UNREALIZED (default, what the LIVE
      runtime computes: realized PnL of today's UTC closes + the full
      unrealized PnL of open positions; limit from CURRENT equity);
  EQUITY_ANCHORED_AT_UTC_MIDNIGHT (sensitivity: equity now minus the marked
      equity at the 00:00 reset, including unrealized PnL carried overnight).
Both reset at an explicit 00:00 UTC event.

Accounting identities are asserted at every step (``AccountingInvariantError``):
  equity = cash + unrealized;  available = equity - committed margin;
  cash = start + realized legs - fees + funding;  margin released once;
  PnL realized once per leg;  no mark or funding after a position is closed.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import random
from typing import Mapping, Sequence

from bot import risk_policy as rp
from bot import nexus_oos_inference as inf
from bot.nexus_oos_execution_parity import replay_size_entry, exit_parity_status, pretrade_parity_status

BAR_MS = 15 * 60 * 1000
DAY_MS = 24 * 3600 * 1000


class AccountingInvariantError(AssertionError):
    """The replay's accounting broke an identity; the result is invalid."""


@dataclass(frozen=True)
class Toggles:
    """Production gates. All True = production parity. Used for attribution."""
    single_position_liquidation_rule: bool = True
    pilot_concurrent_cap: bool = True
    correlation_groups: bool = True
    cooldown_after_close: bool = True
    per_symbol_circuit_breaker: bool = True
    daily_target_conservative_mode: bool = True
    exposure_capacity: bool = True
    production_rank_ordering: bool = True


def _day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


class _Pos:
    __slots__ = ("row", "symbol", "direction", "s", "fill", "qty0", "qty", "margin0", "margin",
                 "entry_ts", "legs", "li", "marks", "mi", "mark", "funding", "fi", "fee_rate",
                 "realized", "fees", "funding_paid", "risk_at_stop", "exit_ts", "planned_risk_quote",
                 "leverage", "exit_reasons", "resolved")

    def __init__(self, row, qty, leverage, fee_rate):
        self.row = row
        self.symbol = row["symbol"]
        self.direction = row["direction"]
        self.s = 1.0 if row["direction"] == "LONG" else -1.0
        self.fill = float(row["fill"])
        self.qty0 = self.qty = float(qty)
        self.leverage = float(leverage)
        self.margin0 = self.margin = self.qty * self.fill / self.leverage
        self.entry_ts = int(row["ts"])
        self.legs = sorted(row["legs"], key=lambda x: int(x[0]))
        self.li = 0
        self.marks = row.get("marks") or []
        self.mi = 0
        self.mark = self.fill
        self.funding = sorted(row.get("funding") or [], key=lambda x: int(x[0]))
        self.fi = 0
        self.fee_rate = float(fee_rate)
        self.realized = 0.0
        self.fees = 0.0
        self.funding_paid = 0.0
        self.risk_at_stop = 0.0
        self.exit_ts = None
        self.planned_risk_quote = self.qty * abs(float(row["signal_entry"]) - float(row["sl"]))
        self.exit_reasons = []
        self.resolved = row.get("outcome_status", "RESOLVED") == "RESOLVED"

    def unrealized(self) -> float:
        return self.s * (self.mark - self.fill) * self.qty


def _rank_key(row, production: bool):
    if production:
        return (-(float(row.get("score_adjusted", row.get("strategy_score", 0)) or 0)
                  * float(row.get("rr", 0) or 0)), str(row["symbol"]))
    return (0, str(row["symbol"]))


def run_portfolio(rows: Sequence[dict], manifest, *, instruments: Mapping[str, dict],
                  mmr_proxy: Mapping[str, float] | None = None, toggles: Toggles = Toggles(),
                  daily_pnl_semantics: str | None = None, record_curve: bool = True,
                  check_invariants: bool = True, correlation_groups=None,
                  session_penalty=None) -> dict:
    from bot.engine import TradingEngine
    from bot import liquidation
    policy = manifest.risk_policy()
    v = manifest.values
    semantics = daily_pnl_semantics or v["DAILY_PNL_SEMANTICS"]
    corr_groups = correlation_groups if correlation_groups is not None else TradingEngine._CORR_GROUPS
    start = float(v["STARTING_EQUITY"])
    lev = float(v["LEVERAGE"])
    drift = float(v["NEXUS_MAX_SIGNAL_DRIFT_BPS"]) / 10_000.0
    max_pos = int(v["MAX_POSITIONS"])
    pilot_cap = int(v["PILOT_MAX_CONCURRENT_POSITIONS"])

    cands = [r for r in rows if r.get("approved") and r.get("executable")]
    if not cands:
        return _empty_report(manifest, toggles, semantics)
    by_ts: dict[int, list] = defaultdict(list)
    for r in cands:
        by_ts[int(r["ts"])].append(r)
    for ts in by_ts:
        by_ts[ts].sort(key=lambda r: _rank_key(r, toggles.production_rank_ordering))

    # Grid anchored on the first decision (exchange bars are 15m aligned, so
    # the grid is too); every candidate, leg and mark lies on this grid.
    t0 = min(by_ts)
    t_end = max([max(by_ts)]
                + [int(l[0]) for r in cands for l in r["legs"]]
                + [int(m[0]) for r in cands for m in (r.get("marks") or [])[-1:]]
                + [int(r["censor_ts"]) for r in cands if r.get("censor_ts")])
    off_grid = [r["symbol"] for r in cands
                if (int(r["ts"]) - t0) % BAR_MS
                or any((int(l[0]) - t0) % BAR_MS for l in r["legs"])]
    if off_grid:
        raise AccountingInvariantError(f"events off the 15m grid: {sorted(set(off_grid))[:5]}")

    cash = start
    ledger = {"realized": 0.0, "fees": 0.0, "funding": 0.0}
    peak = start
    open_pos: dict[str, _Pos] = {}
    trades: list[dict] = []
    skipped = defaultdict(int)
    curve: list[tuple[int, float]] = []
    daily_curve: list[tuple[int, float]] = []
    cooldown_until: dict[str, int] = {}
    consec_losses: dict[str, int] = defaultdict(int)
    realized_today = 0.0
    day_idx = t0 // DAY_MS
    day_start_equity = start
    day_stopped = False
    target_hit = False
    stop_days: set[str] = set()
    target_days: set[str] = set()
    dd_gate_bars = 0
    max_concurrent = 0
    exposure_ms = 0
    margin_time = 0.0
    last_eq = start
    invariant_checks = 0

    def equity_now() -> float:
        return cash + sum(p.unrealized() for p in open_pos.values())

    def check(ts):
        nonlocal invariant_checks
        if not check_invariants:
            return
        invariant_checks += 1
        eq = equity_now()
        expected_cash = start + ledger["realized"] - ledger["fees"] + ledger["funding"]
        scale = max(1.0, abs(start))
        if abs(cash - expected_cash) > 1e-7 * scale:
            raise AccountingInvariantError(f"cash ledger mismatch at {ts}: {cash} vs {expected_cash}")
        for p in open_pos.values():
            if p.qty < -1e-12 or p.margin < -1e-12:
                raise AccountingInvariantError(f"negative qty/margin {p.symbol} at {ts}")
            want = p.qty * p.fill / p.leverage
            if abs(p.margin - want) > 1e-9 * max(1.0, p.margin0):
                raise AccountingInvariantError(f"margin not pro rata {p.symbol} at {ts}")
            if p.qty <= 1e-12:
                raise AccountingInvariantError(f"closed position still open {p.symbol} at {ts}")
        avail = eq - sum(p.margin for p in open_pos.values())
        if not math.isfinite(eq) or not math.isfinite(avail):
            raise AccountingInvariantError(f"non-finite equity at {ts}")

    T = t0
    while T <= t_end:
        # 1. FUNDING on quantity open at the settlement time
        for p in list(open_pos.values()):
            while p.fi < len(p.funding) and int(p.funding[p.fi][0]) <= T:
                f_ts, rate, px = p.funding[p.fi]
                p.fi += 1
                if int(f_ts) <= p.entry_ts:
                    continue
                if p.qty <= 0:
                    raise AccountingInvariantError("funding after close")
                amt = -p.s * float(rate) * float(px) * p.qty
                cash += amt
                ledger["funding"] += amt
                p.funding_paid += amt
                realized_today += amt

        # 2. EXITS (legs in the bar ending at T)
        for sym in sorted(open_pos):
            p = open_pos[sym]
            while p.li < len(p.legs) and int(p.legs[p.li][0]) <= T:
                l_ts, w, px, reason = p.legs[p.li]
                p.li += 1
                q = min(p.qty, float(w) * p.qty0)
                if p.li == len(p.legs) and p.resolved:
                    q = p.qty            # final leg of a RESOLVED trade closes the remainder
                pnl = p.s * (float(px) - p.fill) * q
                fee = p.fee_rate * float(px) * q
                cash += pnl - fee
                ledger["realized"] += pnl
                ledger["fees"] += fee
                p.realized += pnl
                p.fees += fee
                realized_today += pnl - fee
                remaining = p.qty - q
                p.margin = p.margin * (remaining / p.qty) if p.qty > 0 else 0.0
                p.qty = remaining
                p.exit_reasons.append(reason)
                if p.qty <= 1e-12:
                    p.qty = 0.0
                    p.margin = 0.0
                    p.exit_ts = T
                    net = p.realized - p.fees + p.funding_paid
                    trades.append(_trade_record(p, net))
                    del open_pos[sym]
                    if toggles.cooldown_after_close:
                        cooldown_until[sym] = max(cooldown_until.get(sym, 0),
                                                  T + int(v["ENTRY_COOLDOWN_SECONDS"]) * 1000)
                    trade_net_ex_funding = p.realized - p.fees
                    if trade_net_ex_funding < 0:
                        consec_losses[sym] += 1
                        if (toggles.per_symbol_circuit_breaker
                                and consec_losses[sym] >= int(v["MAX_CONSEC_LOSSES"])):
                            cooldown_until[sym] = max(cooldown_until.get(sym, 0),
                                                      T + int(v["CB_COOLDOWN_HOURS"]) * 3600 * 1000)
                    else:
                        consec_losses[sym] = 0
                    break

        # 3. MARK remaining positions at the close of the bar ending at T
        for p in open_pos.values():
            while p.mi < len(p.marks) and int(p.marks[p.mi][0]) <= T:
                p.mark = float(p.marks[p.mi][1])
                p.mi += 1

        eq = equity_now()
        # 4. DAILY_RESET at 00:00 UTC (first grid point of a new UTC day; for
        # 15m-aligned exchange data exactly T == 00:00)
        if T // DAY_MS != day_idx:
            day_idx = T // DAY_MS
            realized_today = 0.0
            day_start_equity = eq
            day_stopped = False
            target_hit = False
            daily_curve.append((T, eq))

        # 5. ACCOUNT / risk state
        if last_eq is not None and open_pos:
            exposure_ms += BAR_MS
            margin_time += BAR_MS * sum(p.margin for p in open_pos.values()) / max(eq, 1e-12)
        last_eq = eq
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 1.0
        if record_curve:
            curve.append((T, eq))
        if semantics == "EQUITY_ANCHORED_AT_UTC_MIDNIGHT":
            daily_pnl = eq - day_start_equity
        else:
            daily_pnl = realized_today + sum(p.unrealized() for p in open_pos.values())
        try:
            limit = rp.effective_daily_stop_limit(eq, policy.daily_stop_loss_pct,
                                                  policy.daily_stop_loss_abs).limit if eq > 0 else 0.0
        except rp.RiskPolicyError:
            limit = 0.0
        if not day_stopped and daily_pnl <= -limit:
            day_stopped = True
            stop_days.add(_day(T))
        if toggles.daily_target_conservative_mode and not target_hit:
            target = (float(v["DAILY_TARGET"]) if float(v["DAILY_TARGET"]) > 0
                      else round(eq * float(v["DAILY_TARGET_PCT"]), 2))
            if realized_today >= target:
                target_hit = True
                target_days.add(_day(T))
        dd_ok = rp.drawdown_entry_decision(dd, policy.max_drawdown).can_open
        if not dd_ok:
            dd_gate_bars += 1
        check(T)

        # 6-7. CANDIDATES at T, production rank order
        for row in by_ts.get(T, ()):
            sym = row["symbol"]
            if day_stopped:
                skipped["daily_stop"] += 1
                continue
            if not dd_ok:
                skipped["drawdown_hard_gate"] += 1
                continue
            if len(open_pos) >= max_pos:
                skipped["position_limit"] += 1
                continue
            if sym in open_pos:
                skipped["same_symbol_open"] += 1
                continue
            if toggles.correlation_groups and any(
                    sym in g and any(o != sym and o in g for o in open_pos) for g in corr_groups):
                skipped["correlation_group"] += 1
                continue
            if cooldown_until.get(sym, 0) > T:
                skipped["cooldown_or_circuit_breaker"] += 1
                continue
            eff_min = (int(v["POST_TARGET_SCORE"]) if target_hit else int(v["MIN_ENTRY_SCORE"]))
            if (int(row.get("strategy_score", 0) or 0) < eff_min
                    or int(row.get("score_adjusted", 0) or 0) < eff_min):
                skipped["score_below_effective_min"] += 1
                continue
            eq = equity_now()
            if eq <= 0:
                skipped["balance_nonpositive"] += 1
                continue
            if toggles.pilot_concurrent_cap and len(open_pos) >= pilot_cap:
                skipped["pilot_concurrent_cap"] += 1
                continue
            used = sum(p.margin for p in open_pos.values())
            available = eq - used
            if toggles.exposure_capacity:
                if available / eq < float(v["PILOT_MIN_AVAILABLE_EQUITY_RATIO"]):
                    skipped["exposure_capacity_available"] += 1
                    continue
                if used / eq > float(v["PILOT_MAX_POSITION_MARGIN_EQUITY_RATIO"]):
                    skipped["exposure_capacity_margin"] += 1
                    continue
            info = instruments.get(sym)
            if info is None:
                skipped["instrument_metadata_missing"] += 1
                continue
            risk_pct = min(float(v["POST_TARGET_RISK"]) if target_hit else float(v["MAX_RISK_PCT"]),
                           float(v["MAX_RISK_PCT"]))
            size = replay_size_entry(
                policy=policy, info=info, equity=eq, available=available,
                signal_entry=float(row["signal_entry"]), signal_sl=float(row["sl"]),
                direction=row["direction"], cost_fraction=float(row["cost_fraction"]),
                risk_pct=risk_pct, max_adverse_entry_drift=drift,
                open_risks=[rp.OpenRisk(p.symbol, p.risk_at_stop) for p in open_pos.values()],
            )
            if size["qty"] <= 0:
                skipped[f"sizing:{size['reason']}"] += 1
                continue
            if toggles.single_position_liquidation_rule and len(open_pos) >= 1:
                skipped["liquidation_guard_multi_position"] += 1
                continue
            mmr = (mmr_proxy or {}).get(sym)
            # Production: n_open_positions = len(positions) + 1 (a second position is
            # never stop-effective). With the rule toggled off (attribution only)
            # the per-position check is evaluated as if isolated.
            n_open = (len(open_pos) + 1) if toggles.single_position_liquidation_rule else 1
            liq_ok = _liquidation_effective(liquidation, row, lev, mmr, n_open=n_open)
            if not liq_ok:
                skipped["liquidation_guard_stop_ineffective"] += 1
                continue
            p = _Pos(row, size["qty"], lev, row["fee_rate"])
            p.risk_at_stop = float(size.get("projected_loss_at_stop") or 0.0)
            fee = p.fee_rate * p.fill * p.qty
            cash -= fee
            ledger["fees"] += fee
            p.fees += fee
            realized_today -= fee
            open_pos[sym] = p
            max_concurrent = max(max_concurrent, len(open_pos))
        check(T)
        T += BAR_MS

    # Unresolved (right-censored) positions are NOT force-closed: no exit leg,
    # no exit fee, no realized PnL. They are reported open and marked.
    for p in open_pos.values():
        if p.resolved:
            raise AccountingInvariantError(f"resolved position left open after last leg: {p.symbol}")
    end_state = _end_state(open_pos, cash, start, [c for c in cands], trades)
    return _report(trades, skipped, curve, daily_curve, manifest, toggles, semantics, start,
                   cands, exposure_ms, margin_time, max_concurrent, stop_days, target_days,
                   dd_gate_bars, invariant_checks, end_state)


def _end_state(open_pos, cash, start, cands, trades) -> dict:
    unreal = sum(p.unrealized() for p in open_pos.values())

    def bound(level_of):
        total = 0.0
        for p in open_pos.values():
            lvl = level_of(p)
            if lvl is None:
                return None
            total += p.s * (float(lvl) - p.fill) * p.qty - p.fee_rate * float(lvl) * p.qty
        return cash + total

    worst = bound(lambda p: p.row.get("native_sl_at_censor"))
    best = bound(lambda p: p.row.get("native_tp"))
    last_candidate = max((int(c["ts"]) for c in cands), default=None)
    first_censor = min((int(p.row["censor_ts"]) for p in open_pos.values()
                        if p.row.get("censor_ts")), default=None)
    path_unknown = bool(first_censor is not None and last_candidate is not None
                        and first_censor < last_candidate)
    sign_flip = (worst is not None and best is not None and ((worst - start) > 0) != ((best - start) > 0))
    return {
        "open_positions_at_end": len(open_pos),
        "open_position_symbols": sorted(open_pos),
        "unrealized_pnl_at_end": unreal,
        "realized_equity_end": cash,
        "realized_pnl": cash - start,
        "marked_final_equity": cash + unreal,
        "final_equity_worst_bound": worst,
        "final_equity_best_bound": best,
        "censored_before_last_candidate": path_unknown,
        # Predeclared: material if an unresolved position was still open before
        # the last candidate (later path unknowable) or if closing the open
        # positions at their native stop vs native TP flips the sign of return.
        "portfolio_censoring_material": bool(path_unknown or sign_flip or (open_pos and worst is None)),
    }


def _liquidation_effective(liquidation, row, leverage, mmr, *, n_open: int) -> bool:
    symbol = str(row["symbol"])
    had = symbol in liquidation._MMR_BY_SYMBOL
    old = (liquidation._MMR_BY_SYMBOL.get(symbol), liquidation._MMR_SOURCE.get(symbol))
    try:
        if mmr is not None:
            liquidation.set_mmr_from_api(symbol, float(mmr), source="research_public_proxy")
        a = liquidation.analyze(entry=float(row["signal_entry"]), stop=float(row["sl"]),
                                leverage=int(leverage), is_long=row["direction"] == "LONG",
                                symbol=symbol, n_open_positions=int(n_open))
    finally:
        if had:
            liquidation._MMR_BY_SYMBOL[symbol], liquidation._MMR_SOURCE[symbol] = old
        else:
            liquidation._MMR_BY_SYMBOL.pop(symbol, None)
            liquidation._MMR_SOURCE.pop(symbol, None)
    return bool(a.stop_effective)


def _trade_record(p: _Pos, net: float) -> dict:
    r_quote = (net / p.planned_risk_quote) if p.planned_risk_quote > 0 else None
    return {"symbol": p.symbol, "direction": p.direction, "entry_ts": p.entry_ts,
            "exit_ts": p.exit_ts, "qty": p.qty0, "pnl": net, "realized_gross": p.realized,
            "fees": p.fees, "funding": p.funding_paid, "r": r_quote,
            "exit_reasons": list(p.exit_reasons), "month": p.row.get("month"),
            "production_regime": p.row.get("production_regime"),
            "research_regime": p.row.get("research_regime"),
            "censored": p.row.get("censored")}


def _drawdown(curve):
    peak, peak_ts = None, None
    mdd, mdd_dur, cur_start = 0.0, 0, None
    for ts, eq in curve:
        if peak is None or eq >= peak:
            if cur_start is not None:
                mdd_dur = max(mdd_dur, ts - cur_start)
            peak, peak_ts, cur_start = eq, ts, None
        else:
            cur_start = cur_start or peak_ts
            mdd = max(mdd, (peak - eq) / peak if peak > 0 else 1.0)
    if cur_start is not None and curve:
        mdd_dur = max(mdd_dur, curve[-1][0] - cur_start)
    return mdd, mdd_dur


def _group(trades, key):
    groups = defaultdict(list)
    for t in trades:
        groups[str(t.get(key))].append(t)
    return {k: {"trades": len(g), "net_pnl": sum(t["pnl"] for t in g),
                "avg_r": (sum(t["r"] for t in g if t["r"] is not None) / len(g))}
            for k, g in sorted(groups.items())}


def _parity_block() -> dict:
    ex, pre = exit_parity_status(), pretrade_parity_status()
    return {
        "exit_parity_complete": ex["complete"],
        "exit_parity_blocking_rules": ex["blocking_rules"],
        "pretrade_parity_complete": pre["complete"],
        "pretrade_parity_blocking_gates": pre["blocking_rules"],
        "portfolio_parity_complete": bool(ex["complete"] and pre["complete"]),
        "status": ("PORTFOLIO_PARITY_COMPLETE" if ex["complete"] and pre["complete"]
                   else "PORTFOLIO_PARITY_INCOMPLETE"),
    }


def _empty_report(manifest, toggles, semantics):
    start = float(manifest.values["STARTING_EQUITY"])
    out = _report([], {}, [], [], manifest, toggles, semantics, start, [], 0, 0.0, 0, set(), set(),
                  0, 0, _end_state({}, start, start, [], []))
    return out


def _report(trades, skipped, curve, daily_curve, manifest, toggles, semantics, start, cands,
            exposure_ms, margin_time, max_concurrent, stop_days, target_days, dd_gate_bars,
            invariant_checks, end_state):
    v = manifest.values
    realized_end = start + sum(t["pnl"] for t in trades)
    # Marked final equity: realized + open (unresolved) positions marked, and
    # any realized partial legs / fees / funding of open positions.
    end = float(end_state["marked_final_equity"])
    trades_sorted = sorted(trades, key=lambda t: (t["exit_ts"], t["symbol"]))
    rs = [t["r"] for t in trades_sorted if t["r"] is not None]
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [-t["pnl"] for t in trades if t["pnl"] < 0]
    loss_streak = cur = 0
    for t in trades_sorted:
        cur = cur + 1 if t["pnl"] < 0 else 0
        loss_streak = max(loss_streak, cur)
    mdd, mdd_dur = _drawdown(curve if curve else [(0, start)])
    span = (curve[-1][0] - curve[0][0]) if len(curve) > 1 else 0
    exit_mix = defaultdict(int)
    for t in trades:
        exit_mix[t["exit_reasons"][-1] if t["exit_reasons"] else "NONE"] += 1
    trade_rows = [{"ts": t["entry_ts"], "r": t["r"], "symbol": t["symbol"]}
                  for t in trades if t["r"] is not None]
    approx_ci = inf.dependence_aware_mean(trade_rows, samples=1000) if len(trade_rows) >= 2 else {}
    return {
        "layer": "PORTFOLIO_EXECUTION_REPLAY",
        "engine": "BAR_BY_BAR_EVENT_ENGINE_V2",
        "event_precedence": ["FUNDING", "EXITS", "MARK", "DAILY_RESET_00UTC", "ACCOUNT",
                             "CANDIDATES(score*rr desc, symbol)", "ENTRY"],
        "daily_pnl_semantics": semantics,
        "policy_sha256": manifest.sha256,
        "toggles": toggles.__dict__,
        "parity": _parity_block(),
        "contract_metadata": v["CONTRACT_SPEC_SOURCE"],
        "starting_equity": start,
        "ending_equity": end,
        "ending_equity_definition": "MARKED_FINAL_EQUITY (realized + open positions marked; no forced close)",
        "net_return": (end - start) / start,
        "realized_return": (end_state["realized_equity_end"] - start) / start,
        "closed_trades_pnl": realized_end - start,
        "end_state": end_state,
        "effective_live_max_concurrent_positions": 1 if toggles.single_position_liquidation_rule else None,
        "configured_max_positions": int(v["MAX_POSITIONS"]),
        "approved_candidates": len(cands),
        "total_trades": len(trades),
        "skipped": dict(sorted(skipped.items())),
        "skipped_position_limit": sum(skipped.get(k, 0) for k in (
            "position_limit", "same_symbol_open", "pilot_concurrent_cap",
            "liquidation_guard_multi_position")),
        "skipped_capital_risk": sum(n for k, n in skipped.items() if k.startswith("sizing:")),
        "blocked_daily_stop": skipped.get("daily_stop", 0),
        "blocked_drawdown": skipped.get("drawdown_hard_gate", 0),
        "daily_stop_days": len(stop_days),
        "daily_target_days": len(target_days),
        "drawdown_gate_active_bars": dd_gate_bars,
        "total_fees": sum(t["fees"] for t in trades),
        "total_funding": sum(t["funding"] for t in trades),
        "portfolio_max_drawdown": mdd,
        "portfolio_max_drawdown_duration_hours": mdd_dur / inf.HOUR_MS,
        "research_max_drawdown_limit": float(v["RESEARCH_MAX_DRAWDOWN_LIMIT"]),
        "profit_factor": (sum(wins) / sum(losses)) if losses else None,
        "net_expectancy_r": (sum(rs) / len(rs)) if rs else None,
        "net_expectancy_quote": ((end - start) / len(trades)) if trades else None,
        "win_rate": (len(wins) / len(trades)) if trades else None,
        "avg_winner_r": (sum(r for r in rs if r > 0) / len([r for r in rs if r > 0])) if any(r > 0 for r in rs) else None,
        "avg_loser_r": (sum(r for r in rs if r < 0) / len([r for r in rs if r < 0])) if any(r < 0 for r in rs) else None,
        "longest_losing_streak": loss_streak,
        "exposure_time_fraction": (exposure_ms / span) if span > 0 else None,
        "avg_capital_utilization": (margin_time / span) if span > 0 else None,
        "max_concurrent_positions": max_concurrent,
        "exit_reason_mix": dict(sorted(exit_mix.items())),
        "censored_trades": sum(1 for t in trades if t.get("censored")),
        "approximate_trade_level_ci": {
            **approx_ci, "authority": "NONE",
            "note": ("Resampling accepted trades after the fact does not re-run the path-dependent "
                     "portfolio; reported for comparison only. Authority: path_bootstrap."),
        },
        "by_symbol": _group(trades, "symbol"),
        "by_production_regime": _group(trades, "production_regime"),
        "by_research_regime": _group(trades, "research_regime"),
        "by_direction": _group(trades, "direction"),
        "by_month": _group(trades, "month"),
        "contributing_symbols": sum(1 for g in _group(trades, "symbol").values() if g["net_pnl"] > 0),
        "equity_curve_daily": [[ts, round(eq, 6)] for ts, eq in daily_curve],
        "accounting_invariant_checks": invariant_checks,
        "accounting_invariants": "PASS",
    }


# ── path bootstrap: DIAGNOSTIC ONLY ─────────────────────────────────────────
PATH_BOOTSTRAP_STATUS = "APPROXIMATE_NON_AUTHORITATIVE"
PATH_BOOTSTRAP_REASON = (
    "Candidate blocks are resampled together with their precomputed future "
    "(legs, marks, funding). A position from source block A can extend into a "
    "synthetic neighbouring block whose candidates come from an unrelated source "
    "block B, so the synthetic market timeline is not internally coherent. The "
    "interval is reported for comparison only and never carries authority.")


def _shift_row(row: dict, dt: int) -> dict:
    out = dict(row)
    out["ts"] = int(row["ts"]) + dt
    out["legs"] = [(int(t) + dt, w, p, why) for t, w, p, why in row["legs"]]
    out["marks"] = [(int(t) + dt, c) for t, c in (row.get("marks") or [])]
    out["funding"] = [(int(t) + dt, r, p) for t, r, p in (row.get("funding") or [])]
    if row.get("censor_ts"):
        out["censor_ts"] = int(row["censor_ts"]) + dt
    return out


def path_bootstrap(rows: Sequence[dict], manifest, *, instruments, mmr_proxy=None,
                   block_ms: int = DAY_MS, replicates: int = 200, seed: int = 11,
                   toggles: Toggles = Toggles()) -> dict:
    """Calendar-block resampling of the CANDIDATE timeline, re-run through the
    state machine. APPROXIMATE_NON_AUTHORITATIVE (see PATH_BOOTSTRAP_REASON)."""
    cands = [r for r in rows if r.get("approved") and r.get("executable")]
    base = {"status": PATH_BOOTSTRAP_STATUS, "authority": "NONE", "reason": PATH_BOOTSTRAP_REASON,
            "block_hours": block_ms // inf.HOUR_MS}
    if not cands:
        return {**base, "replicates": 0, "net_return_ci": [None, None]}
    first = (min(int(r["ts"]) for r in cands) // block_ms) * block_ms
    last = (max(int(r["ts"]) for r in cands) // block_ms) * block_ms
    n_blocks = int((last - first) // block_ms) + 1
    blocks: dict[int, list] = defaultdict(list)
    for r in cands:
        blocks[int((int(r["ts"]) - first) // block_ms)].append(r)
    rng = random.Random(seed)
    returns, expectancies, spliced = [], [], 0
    for _ in range(int(replicates)):
        sample = []
        prev_src = None
        for slot in range(n_blocks):
            src = rng.randrange(n_blocks)
            if prev_src is not None and src != prev_src + 1:
                spliced += 1
            prev_src = src
            dt = (slot - src) * block_ms
            sample.extend(_shift_row(r, dt) for r in blocks.get(src, ()))
        rep = run_portfolio(sample, manifest, instruments=instruments, mmr_proxy=mmr_proxy,
                            toggles=toggles, record_curve=False, check_invariants=False)
        returns.append(rep["net_return"])
        if rep["net_expectancy_r"] is not None:
            expectancies.append(rep["net_expectancy_r"])
    lo, hi = inf._percentile_ci(list(returns))
    elo, ehi = inf._percentile_ci(list(expectancies))
    return {**base, "method": "UTC_BLOCK_RESAMPLED_CANDIDATE_TIMELINE_RERUN_THROUGH_STATE_MACHINE",
            "replicates": len(returns), "net_return_ci": [lo, hi],
            "net_expectancy_r_ci": [elo, ehi], "non_contiguous_block_joins": spliced}


def path_bootstrap_diagnostic(rows, manifest, *, instruments, mmr_proxy=None, replicates=100,
                              block_lengths_ms=(DAY_MS, 3 * DAY_MS, 7 * DAY_MS)) -> dict:
    per = {f"{b // inf.HOUR_MS}h": path_bootstrap(rows, manifest, instruments=instruments,
                                                  mmr_proxy=mmr_proxy, block_ms=b,
                                                  replicates=replicates)
           for b in block_lengths_ms}
    return {"per_block_length": per, "status": PATH_BOOTSTRAP_STATUS, "authority": "NONE",
            "reason": PATH_BOOTSTRAP_REASON}


def _truncate_to_window(row: dict, end_ts: int) -> dict:
    """Cut a trade's market data at ``end_ts`` (exclusive). A trade that has not
    resolved before ``end_ts`` becomes RIGHT_CENSORED_FOLD_END: its later legs,
    marks and funding are dropped, never used by this fold."""
    last_leg = max((int(l[0]) for l in row["legs"]), default=None)
    resolved = row.get("outcome_status", "RESOLVED") == "RESOLVED"
    if resolved and last_leg is not None and last_leg < end_ts:
        return row
    out = dict(row)
    out["legs"] = [l for l in row["legs"] if int(l[0]) < end_ts]
    out["marks"] = [m for m in (row.get("marks") or []) if int(m[0]) < end_ts]
    out["funding"] = [f for f in (row.get("funding") or []) if int(f[0]) < end_ts]
    censor = row.get("censor_ts")
    if resolved or censor is None or int(censor) >= end_ts:
        out["outcome_status"] = "RIGHT_CENSORED_FOLD_END"
        out["censor_ts"] = max([int(row["ts"])] + [int(m[0]) for m in out["marks"]]
                               + [int(l[0]) for l in out["legs"]])
    return out


def _max_market_ts(rows: Sequence[dict]) -> int | None:
    ts = [int(t[0]) for r in rows for key in ("legs", "marks", "funding") for t in (r.get(key) or [])]
    ts += [int(r["censor_ts"]) for r in rows if r.get("censor_ts")]
    ts += [int(r["ts"]) for r in rows]
    return max(ts) if ts else None


def walk_forward_folds(rows, manifest, *, instruments, mmr_proxy=None, folds: int | None = None,
                       required_horizon_ms: int | None = None, decision_span=None) -> dict:
    """PURGED / EMBARGOED portfolio robustness folds (regime robustness).

    Layout = inf.purged_calendar_folds: DECISION_WINDOW + OUTCOME_COMPLETION_WINDOW,
    then EMBARGO, then the next fold. The embargo is >= the required outcome
    horizon (max resolved horizon of the approved trades, ceil whole days, and
    never below ``required_horizon_ms``). Each fold's trades are truncated at
    its outcome-window end, so no fold uses market time of the next fold.
    Each fold restarts from STARTING_EQUITY: fold_account_state =
    RESET_FOR_REGIME_ROBUSTNESS (this is NOT a continuous portfolio replay).
    If four folds cannot fit: INSUFFICIENT_INDEPENDENT_PORTFOLIO_PERIODS.
    """
    k = folds or inf.FOLD_COUNT
    cands = sorted((r for r in rows if r.get("approved") and r.get("executable")),
                   key=lambda r: int(r["ts"]))
    own = inf.required_block_ms([{"ts": r["ts"], "outcome_end_ts": max(int(l[0]) for l in r["legs"]),
                                  "outcome_status": "RESOLVED"}
                                 for r in cands
                                 if r.get("outcome_status", "RESOLVED") == "RESOLVED" and r["legs"]])
    req = max(own, int(required_horizon_ms or 0))
    base = {"method": "PURGED_EMBARGOED_CALENDAR_FOLDS",
            "fold_account_state": "RESET_FOR_REGIME_ROBUSTNESS",
            "not_a_continuous_backtest": True, "authority": "PORTFOLIO_ROBUSTNESS",
            "rule": "a fold counts as positive only if marked AND realized return > 0 and its "
                    "censoring is not material; folds must be overlap-free"}
    if not cands:
        return {**base, "status": "INSUFFICIENT_INDEPENDENT_PORTFOLIO_PERIODS", "folds": [],
                "folds_total": 0, "folds_positive": 0, "folds_authoritative": 0,
                "folds_overlap_free": False, "fold_embargo_ms": req, "fold_required_horizon_ms": req}
    t0, t1 = decision_span or (int(cands[0]["ts"]), int(cands[-1]["ts"]) + 1)
    layout = inf.purged_calendar_folds(t0, t1, required_horizon_ms=req, folds=k)
    status = ("OK" if layout["status"] == "OK" else "INSUFFICIENT_INDEPENDENT_PORTFOLIO_PERIODS")
    out = []
    for w in layout["windows"]:
        part = [_truncate_to_window(r, w["outcome_window_end_ts"]) for r in cands
                if w["decision_start_ts"] <= int(r["ts"]) < w["decision_end_ts"]]
        rep = run_portfolio(part, manifest, instruments=instruments, mmr_proxy=mmr_proxy,
                            record_curve=True, check_invariants=True)
        out.append({**w, "candidates": len(part), "trades": rep["total_trades"],
                    "net_return_marked": rep["net_return"],
                    "realized_return": rep["realized_return"],
                    "open_positions_at_end": rep["end_state"]["open_positions_at_end"],
                    "censoring_material": rep["end_state"]["portfolio_censoring_material"],
                    "max_drawdown": rep["portfolio_max_drawdown"],
                    "max_market_ts_used": _max_market_ts(part)})
    try:
        overlap_free = inf.assert_folds_overlap_free(out) if out else False
    except AssertionError:
        overlap_free = False
    positive = sum(1 for f in out if f["net_return_marked"] > 0 and f["realized_return"] > 0
                   and not f["censoring_material"])
    authoritative = status == "OK" and overlap_free
    return {**base, "status": status, "layout": {k_: v for k_, v in layout.items() if k_ != "windows"},
            "fold_embargo_ms": layout["fold_embargo_ms"],
            "fold_required_horizon_ms": layout["fold_required_horizon_ms"],
            "folds": out, "folds_total": len(out), "folds_overlap_free": bool(overlap_free),
            "folds_authoritative": len(out) if authoritative else 0,
            "folds_positive": positive if authoritative else 0}


def contract_spec_sensitivity(rows, manifest, *, instruments, mmr_proxy=None) -> dict:
    """CURRENT_CONTRACT_SPEC_PROXY sensitivity: perturb lot/multiplier/minimum
    notional/MMR around the current public specs and re-run the portfolio."""
    def perturb(fn):
        return {s: fn(dict(info)) for s, info in instruments.items()}

    def lot_x10(i):
        i["lotSize"] = i["qtyStep"] = float(i["lotSize"]) * 10
        i["minQty"] = max(float(i["minQty"]), float(i["lotSize"]))
        return i

    def mult_x10(i):
        i["multiplier"] = float(i["multiplier"]) * 10
        return i

    def min_notional_10(i):
        i["minNotional"] = max(float(i.get("minNotional", 0) or 0), 10.0)
        return i

    def mmr_x2(i):
        if "contractMaintainMarginReference" in i:
            i["contractMaintainMarginReference"] = min(0.5, float(i["contractMaintainMarginReference"]) * 2)
        return i

    scenarios = {
        "current": (instruments, mmr_proxy),
        "lot_size_x10": (perturb(lot_x10), mmr_proxy),
        "multiplier_x10": (perturb(mult_x10), mmr_proxy),
        "min_notional_10usdt": (perturb(min_notional_10), mmr_proxy),
        "mmr_x2": (perturb(mmr_x2), {k: min(0.5, x * 2) for k, x in (mmr_proxy or {}).items()}),
    }
    out = {}
    for name, (inst, mmr) in scenarios.items():
        rep = run_portfolio(rows, manifest, instruments=inst, mmr_proxy=mmr, record_curve=True,
                            check_invariants=False)
        out[name] = {k: rep[k] for k in ("ending_equity", "net_return", "total_trades",
                                         "portfolio_max_drawdown", "net_expectancy_r",
                                         "skipped_capital_risk")}
    base = out["current"]
    out["conclusion_changes"] = any(
        (o["net_return"] > 0) != (base["net_return"] > 0) for k, o in out.items() if k != "current")
    out["contract_metadata"] = "CURRENT_CONTRACT_SPEC_PROXY"
    out["historical_specs_available"] = False
    return out
