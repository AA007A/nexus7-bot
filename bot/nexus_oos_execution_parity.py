"""Production execution parity for the NEXUS OOS research replay (research only).

This module never sends orders, reads credentials or mutates runtime state. It
reproduces, at 15m-bar resolution, what the composed LIVE-pilot runtime does
between a strategy signal and a closed trade, and classifies every
execution-authoritative rule by how faithfully the replay can reproduce it.

The production facts below were established by reading the composed runtime
(``bot.runtime_bootstrap`` install order), not by assumption:

* Signal geometry. ``strategy.calc_sl_tp`` (distinct TP1/TP2) has no caller.
  ``Analyzer.analyze_mtf`` and the adaptive-MTF wrapper build ``Signal``
  without ``tp1``/``tp2`` and ``Signal.__post_init__`` sets ``tp1 = tp2 = tp``.
  TP1 == TP2 is therefore production reality, not a replay artefact.
* Partial TP. ``_manage_partial_tp`` ignores ``sig.tp1``: it closes 50% of the
  original quantity at market once price reaches ``entry +/- |entry - sl| +/-
  entry * 0.0003`` (1R plus a funding buffer) and moves the stop to entry.
* Native protection. The entry order carries exchange-native TP/SL triggers at
  the UNSHIFTED signal levels (``kucoin_native_tpsl``). After the fill the
  engine shifts its LOCAL entry/sl/tp by the fill delta, but the exchange stop
  stays at the signal level until a later ``set_sl`` moves it.
* Trailing. ``trailing_safety_hardening`` gives back ``TRAILING_LOCK`` of the
  peak favourable excursion once pnl >= ``TRAILING_TRIGGER`` x |tp - entry|.
  ``peak_pnl`` is not rescaled after the partial, so the excursion is read at
  twice its size; ``native_stop_repair.set_stops`` rejects a stop on the wrong
  side of the mark, in which case the stop is unchanged.
* 2R exit. ``confirmed_rr_exit.check`` closes 100% at market when profit >=
  2 x |entry - CURRENT stop|; after break-even the distance is 0 and it is
  inert.
* Stagnation / CHoCH / regime exits are DISABLED in LIVE:
  ``operator_loss_policy`` (installed last) replaces
  ``_check_stagnation_and_invalidation`` with a no-op for the LIVE pilot.
* ``Position.min_hold_until`` (90 min) is telemetry only; it never blocks an
  exit.
* There is no time exit in production. The historical replay's 40-bar exit
  had no production counterpart.
* Post-NEXUS geometry (``kucoin_contract_risk_hardening``): with no open
  position, a stop beyond the liquidation-safe distance is compressed
  proportionally (R:R preserved) when >= 40% of the stop distance remains and
  fee viability survives; NEXUS is re-run on the adjusted levels. Otherwise
  the entry is blocked.
* Single position. ``engine._open`` calls ``liquidation.analyze`` with
  ``n_open_positions = len(positions) + 1``; any value > 1 is
  ``stop_effective = False`` and ``ALLOW_SL_BEYOND_LIQUIDATION`` is forbidden by
  ``liquidation_override_guard``. A second concurrent LIVE position is
  therefore always rejected; the CROSS portfolio stress gate is unreachable.
* Legacy pre-trade score (``score.calculate``) is ADVISORY after an exact
  NEXUS approval in the LIVE pilot (``legacy_pretrade_advisory``); only an
  explicit ``hard_block`` (official CROSS MMR unavailable) keeps authority.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import math
from typing import Sequence

FIFTEEN_MIN_MS = 15 * 60 * 1000

# ── classification vocabulary ───────────────────────────────────────────────
FULLY_REPLAYED = "FULLY_REPLAYED"
APPROXIMATED = "APPROXIMATED"
NOT_REPLAYABLE = "NOT_REPLAYABLE"
LIVE_ONLY = "LIVE_ONLY"
INACTIVE_IN_LIVE = "INACTIVE_IN_LIVE"          # rule exists in code, disabled for LIVE
TELEMETRY_ONLY = "TELEMETRY_ONLY"              # no authorization/exit effect
NOT_AUTHORITATIVE_IN_LIVE = "NOT_AUTHORITATIVE_IN_LIVE"
UNREACHABLE = "UNREACHABLE"

# Outcome status of a simulated trade. Only RESOLVED outcomes are completed
# trades; right-censored outcomes never receive a realized R.
RESOLVED = "RESOLVED"
RIGHT_CENSORED_DATA_END = "RIGHT_CENSORED_DATA_END"
RIGHT_CENSORED_RESEARCH_LIMIT = "RIGHT_CENSORED_RESEARCH_LIMIT"

# A rule "blocks parity" when it can change an authorization/exit and the
# replay cannot reproduce it exactly (and no conservative equivalent is proven).
_PARITY_BLOCKING = {APPROXIMATED, NOT_REPLAYABLE, LIVE_ONLY}


EXIT_PARITY_MATRIX: tuple[dict, ...] = (
    {"rule": "native_hard_sl", "owner": "kucoin_native_tpsl (entry order trigger)",
     "execution_authoritative": True, "classification": APPROXIMATED,
     "replay": "exchange stop at UNSHIFTED signal SL; bar low/high crossing; STOP_FIRST on same-bar "
               "ambiguity; gap through the stop fills at the bar open",
     "why_not_full": "15m bars: intrabar order of extremes unknown"},
    {"rule": "native_tp", "owner": "kucoin_native_tpsl",
     "execution_authoritative": True, "classification": APPROXIMATED,
     "replay": "exchange TP at UNSHIFTED signal TP; processed after the stop (STOP_FIRST)",
     "why_not_full": "15m bars"},
    {"rule": "tp1_partial_50pct", "owner": "engine._manage_partial_tp + partial_tp_execution_hardening",
     "execution_authoritative": True, "classification": APPROXIMATED,
     "replay": "50% at market when price reaches fill +/- (|fill - local_sl| + fill*0.0003); "
               "levels processed in ascending order along the favourable excursion",
     "why_not_full": "5s polling approximated by bar extremes"},
    {"rule": "tp2", "owner": "Signal.__post_init__ (tp2 = tp)", "execution_authoritative": False,
     "classification": FULLY_REPLAYED,
     "replay": "tp2 == tp in production; the remainder exits at native TP / stop / 2R / trailing"},
    {"rule": "break_even_after_tp1", "owner": "engine._manage_partial_tp (set_sl(entry))",
     "execution_authoritative": True, "classification": APPROXIMATED,
     "replay": "native stop moves to the fill price; a same-bar crossing counts only when the bar "
               "CLOSE is beyond the new stop (certain crossing)",
     "why_not_full": "intrabar ordering"},
    {"rule": "trailing_stop", "owner": "engine._apply_trailing_stops + trailing_safety_hardening",
     "execution_authoritative": True, "classification": APPROXIMATED,
     "replay": "peak = bar extreme; production formula incl. un-rescaled peak_pnl after partial; "
               "stop rejected when on the wrong side of the mark (native_stop_repair)",
     "why_not_full": "5s polling approximated by bar extremes"},
    {"rule": "rr_double_2R_exit", "owner": "confirmed_rr_exit.check (LIVE)",
     "execution_authoritative": True, "classification": APPROXIMATED,
     "replay": "100% at market when profit >= 2 x |fill - CURRENT local stop|",
     "why_not_full": "5s polling approximated by bar extremes"},
    {"rule": "stagnation_4h", "owner": "operator_loss_policy.no_discretionary_loss_exit",
     "execution_authoritative": False, "classification": INACTIVE_IN_LIVE,
     "replay": "not applied (disabled for the LIVE pilot)"},
    {"rule": "choch_invalidation", "owner": "operator_loss_policy.no_discretionary_loss_exit",
     "execution_authoritative": False, "classification": INACTIVE_IN_LIVE,
     "replay": "not applied (disabled for the LIVE pilot)"},
    {"rule": "regime_invalidation", "owner": "operator_loss_policy.no_discretionary_loss_exit",
     "execution_authoritative": False, "classification": INACTIVE_IN_LIVE,
     "replay": "not applied (disabled for the LIVE pilot)"},
    {"rule": "signal_invalidation", "owner": "none found in the LIVE exit path",
     "execution_authoritative": False, "classification": INACTIVE_IN_LIVE,
     "replay": "not applied"},
    {"rule": "min_hold_90m", "owner": "Position.min_hold_until (_sync_positions logs only)",
     "execution_authoritative": False, "classification": TELEMETRY_ONLY,
     "replay": "not applied (never blocks an exit)"},
    {"rule": "time_exit", "owner": "none in production", "execution_authoritative": False,
     "classification": FULLY_REPLAYED,
     "replay": "no time exit; unresolved positions are RIGHT_CENSORED (never force-closed)"},
    {"rule": "funding_settlement", "owner": "exchange", "execution_authoritative": True,
     "classification": FULLY_REPLAYED,
     "replay": "charged at actual public funding timepoints on the quantity open at that time"},
    {"rule": "exchange_liquidation", "owner": "exchange (CROSS risk rate)",
     "execution_authoritative": True, "classification": APPROXIMATED,
     "replay": "not simulated; single-position geometry keeps the stop >= MIN_STOP_LIQ_GAP_PCT "
               "inside the (proxy-MMR) liquidation distance",
     "why_not_full": "account-specific CROSS MMR is private/live"},
    {"rule": "restart_durable_state", "owner": "durable_execution / restart ownership guards",
     "execution_authoritative": True, "classification": LIVE_ONLY,
     "replay": "not replayable: depends on process restarts and exchange reconciliation"},
)


PRETRADE_GATE_MATRIX: tuple[dict, ...] = (
    {"gate": "strategy_analyze_mtf (incl. adaptive MTF, pullback, RR precision, closed-candle sentinel)",
     "classification": FULLY_REPLAYED, "can_change_authorization": True,
     "replay": "same Analyzer after runtime_bootstrap.install()"},
    {"gate": "session_score_adjustment", "classification": FULLY_REPLAYED,
     "can_change_authorization": True, "replay": "TradingEngine._SESSION_PENALTY by decision UTC hour"},
    {"gate": "regime_allows_direction", "classification": FULLY_REPLAYED,
     "can_change_authorization": True, "replay": "TradingEngine._REGIME_PARAMS[sig.regime].allowed_sides"},
    {"gate": "expected_pnl_positive", "classification": FULLY_REPLAYED,
     "can_change_authorization": True, "replay": "sig.expected_pnl > 0"},
    {"gate": "effective_min_score_after_daily_target", "classification": FULLY_REPLAYED,
     "can_change_authorization": True, "replay": "portfolio state: POST_TARGET_SCORE after realized >= target"},
    {"gate": "same_symbol_open / correlation_group / cooldown / per-symbol circuit breaker",
     "classification": FULLY_REPLAYED, "can_change_authorization": True, "replay": "portfolio state"},
    {"gate": "rank_signals ordering (score * rr desc)", "classification": FULLY_REPLAYED,
     "can_change_authorization": True, "replay": "portfolio same-bar ordering"},
    {"gate": "nexus_decide", "classification": APPROXIMATED, "can_change_authorization": True,
     "replay": "candles + ticker proxy + public funding; OI / order book / news unavailable",
     "why_not_full": "historical OI and order book not available"},
    {"gate": "cross_geometry_compression + NEXUS re-run", "classification": APPROXIMATED,
     "can_change_authorization": True,
     "replay": "production _geometry_from_exact_mmr with PUBLIC maintainMargin as MMR proxy",
     "why_not_full": "account-specific CROSS MMR is private/live"},
    {"gate": "official_cross_mmr_available (hard_block)", "classification": LIVE_ONLY,
     "can_change_authorization": True, "replay": "assumed available"},
    {"gate": "legacy_pretrade_score (score.calculate)", "classification": NOT_AUTHORITATIVE_IN_LIVE,
     "can_change_authorization": False,
     "replay": "advisory after exact NEXUS approval in the LIVE pilot (legacy_pretrade_advisory)"},
    {"gate": "pilot_guard_operational (auth, WS, integrity, unprotected, ambiguous orders)",
     "classification": LIVE_ONLY, "can_change_authorization": True, "replay": "assumed healthy"},
    {"gate": "pilot_max_concurrent_positions", "classification": FULLY_REPLAYED,
     "can_change_authorization": True, "replay": "manifest pilot_max_concurrent_positions"},
    {"gate": "pilot_session_submission_cap (2 per process session)", "classification": NOT_REPLAYABLE,
     "can_change_authorization": True,
     "replay": "not applied: depends on process restarts; production trades at most 2 new orders "
               "per deploy/restart"},
    {"gate": "pilot_exposure_capacity (available/equity, margin/equity)", "classification": FULLY_REPLAYED,
     "can_change_authorization": True, "replay": "portfolio state (bot positions only; no external positions)"},
    {"gate": "market_risk_runtime (CoinGlass / macro feeds)", "classification": NOT_REPLAYABLE,
     "can_change_authorization": True, "replay": "no historical feed"},
    {"gate": "drawdown_hard_gate / daily_stop / durable daily stop", "classification": FULLY_REPLAYED,
     "can_change_authorization": True, "replay": "portfolio state at every 15m mark"},
    {"gate": "final_sizing_invariants (min of caps, exchange lot/minimum, loss budget)",
     "classification": FULLY_REPLAYED, "can_change_authorization": True,
     "replay": "risk_policy.size_new_entry with production cost model and drift allowance",
     "note": "contract specs are CURRENT_CONTRACT_SPEC_PROXY"},
    {"gate": "liquidation_guard single position (engine._open)", "classification": FULLY_REPLAYED,
     "can_change_authorization": True, "replay": "any open bot position rejects a new entry"},
    {"gate": "liquidation_guard stop_effective", "classification": APPROXIMATED,
     "can_change_authorization": True, "replay": "liquidation.analyze with proxy MMR",
     "why_not_full": "account-specific CROSS MMR"},
    {"gate": "cross_portfolio_stress", "classification": UNREACHABLE,
     "can_change_authorization": False,
     "replay": "only evaluated for a second position, which the liquidation guard rejects first"},
    {"gate": "pre_dispatch signal drift", "classification": APPROXIMATED,
     "can_change_authorization": True,
     "replay": "modeled executable price (next open + slippage) vs signal entry",
     "why_not_full": "no historical top of book"},
    {"gate": "pre_dispatch spread / depth", "classification": NOT_REPLAYABLE,
     "can_change_authorization": True, "replay": "no historical order book"},
    {"gate": "market_viability (24h stats)", "classification": APPROXIMATED,
     "can_change_authorization": True, "replay": "all 12 research symbols assumed viable"},
)


def matrix_status(matrix: Sequence[dict], *, key_field: str) -> dict:
    blocking = [row[key_field] for row in matrix
                if row["classification"] in _PARITY_BLOCKING
                and (row.get("execution_authoritative", row.get("can_change_authorization", True)))]
    return {"complete": not blocking, "blocking_rules": blocking,
            "classifications": {row[key_field]: row["classification"] for row in matrix}}


def exit_parity_status() -> dict:
    return matrix_status(EXIT_PARITY_MATRIX, key_field="rule")


def pretrade_parity_status() -> dict:
    return matrix_status(PRETRADE_GATE_MATRIX, key_field="gate")


# ── candidate-level production funnel ───────────────────────────────────────
def market_session(hour_utc: int) -> str:
    """Same boundaries as TradingEngine._get_market_session (tested for parity)."""
    if 0 <= hour_utc < 8:
        return "ASIA"
    if 8 <= hour_utc < 16:
        return "LONDON"
    return "NEW_YORK"


def funnel_flags(sig, decision_ts_ms: int, engine_cls) -> dict:
    """Post-signal scan funnel of TradingEngine._scan_all_and_enter.

    The session-adjusted score is compared against the EFFECTIVE min score in
    the portfolio (it depends on the daily-target state); regime direction and
    expected PnL are stateless.
    """
    hour = datetime.fromtimestamp(int(decision_ts_ms) / 1000, tz=timezone.utc).hour
    session = market_session(hour)
    penalty = int(engine_cls._SESSION_PENALTY.get(session, {}).get(str(sig.symbol), 0))
    adjusted = max(0, int(getattr(sig, "score", 0) or 0) + penalty)
    regime = getattr(sig, "regime", "RANGING")
    params = engine_cls._REGIME_PARAMS.get(regime, engine_cls._REGIME_PARAMS["RANGING"])
    allowed = str(sig.direction) in params.get("allowed_sides", ["LONG", "SHORT"])
    return {
        "session": session,
        "session_penalty": penalty,
        "adjusted_score": adjusted,
        "regime_allows_direction": bool(allowed),
        "expected_pnl_positive": float(getattr(sig, "expected_pnl", 0.0) or 0.0) > 0,
    }


def drift_gate(signal_entry: float, executable: float, direction: str, max_bps: float) -> dict:
    """pre_dispatch_guard drift rule (adverse drift beyond the limit blocks)."""
    from bot.pre_dispatch_guard import _classify_directional_drift
    side = "BUY" if str(direction).upper() == "LONG" else "SELL"
    signed = (float(executable) - float(signal_entry)) / float(signal_entry) * 10_000.0
    cls = _classify_directional_drift(side, signed)
    blocked = abs(signed) > float(max_bps) and cls != "FAVORABLE_IMPROVEMENT"
    return {"signed_drift_bps": signed, "classification": cls, "blocked": bool(blocked)}


class _SigView:
    """Mutable signal copy for geometry evaluation (never touches the original)."""

    def __init__(self, sig, **overrides):
        for k in ("symbol", "direction", "entry", "sl", "tp", "score", "rr", "expected_pnl",
                  "total_fees", "regime", "entry_type"):
            setattr(self, k, getattr(sig, k, None))
        for k, v in overrides.items():
            setattr(self, k, v)


def production_geometry(sig, *, leverage: int, mmr: float | None, fee_multiplier: float) -> dict:
    """kucoin_contract_risk_hardening geometry for an entry with no open position.

    Uses the production function ``_geometry_from_exact_mmr`` with ``mmr``
    registered as the symbol MMR (proxy for the private CROSS MMR), then the
    same post-compression fee-viability rule. The liquidation MMR registry is
    restored afterwards.
    """
    from bot import liquidation
    from bot.kucoin_contract_risk_hardening import _geometry_from_exact_mmr

    symbol = str(sig.symbol)
    had = symbol in liquidation._MMR_BY_SYMBOL
    old = (liquidation._MMR_BY_SYMBOL.get(symbol), liquidation._MMR_SOURCE.get(symbol))
    try:
        if mmr is not None:
            liquidation.set_mmr_from_api(symbol, float(mmr), source="research_public_proxy")
        else:
            liquidation._MMR_BY_SYMBOL.pop(symbol, None)
        geo = _geometry_from_exact_mmr(liquidation, _SigView(sig), int(leverage))
    finally:
        if had:
            liquidation._MMR_BY_SYMBOL[symbol], liquidation._MMR_SOURCE[symbol] = old
        else:
            liquidation._MMR_BY_SYMBOL.pop(symbol, None)
            liquidation._MMR_SOURCE.pop(symbol, None)
    status = geo.get("status")
    out = {"status": status, "reason": geo.get("reason"),
           "sl": float(sig.sl), "tp": float(sig.tp),
           "original_stop_pct": geo.get("original_stop_pct"),
           "final_stop_pct": geo.get("final_stop_pct"),
           "retained_fraction": geo.get("retained_fraction"),
           "mmr_used": mmr, "mmr_source": "PUBLIC_MAINTAIN_MARGIN_PROXY" if mmr is not None
           else "liquidation.DEFAULT_MMR"}
    if status == "ADJUSTED":
        new_sl = round(float(geo["sl"]), 8)
        new_tp = round(float(geo["tp"]), 8)
        entry = float(sig.entry)
        move_to_tp_pct = abs(new_tp - entry) / entry * 100.0
        total_fees_pct = float(getattr(sig, "total_fees", 0.0) or 0.0)
        if move_to_tp_pct < total_fees_pct * float(fee_multiplier):
            out.update(status="BLOCK", reason="post_compression_fee_viability")
        else:
            out.update(sl=new_sl, tp=new_tp,
                       expected_pnl=round(move_to_tp_pct - total_fees_pct, 3))
    return out


# ── exit policy + bar-level production exit emulation ───────────────────────
@dataclass(frozen=True)
class ExitPolicy:
    partial_fraction: float = 0.5
    tp1_funding_buffer: float = 0.0003       # entry * 0.0001 * 3
    rr_double_multiple: float = 2.0
    trailing_trigger: float = 0.50
    trailing_lock: float = 0.25
    enable_partial: bool = True
    enable_trailing: bool = True
    enable_rr_double: bool = True
    shift_native_stops: bool = False          # research toggle: legacy (wrong) behaviour
    research_max_hold_bars: int = 2880        # 30 days; right-censoring cap, NOT a production exit

    def to_dict(self) -> dict:
        return asdict(self)


def _slip(price: float, direction: str, slippage_rate: float, *, is_entry: bool) -> float:
    from bot.kucoin_execution_model import adverse_fill
    return adverse_fill(price, direction, is_entry=is_entry, slippage_rate=slippage_rate)


def simulate_production_exit(*, direction: str, bars: Sequence[dict], start_idx: int,
                             signal_entry: float, signal_sl: float, signal_tp: float,
                             slippage_rate: float, policy: ExitPolicy = ExitPolicy(),
                             ts_of=None) -> dict | None:
    """Emulate the LIVE exit stack at 15m resolution.

    Entry: market fill at the open of ``bars[start_idx]`` (+ adverse slippage).
    Per bar, deterministic order:
      1. native stop (as of bar start) vs the adverse extreme -> exit
         (a bar that opens beyond the stop fills at the open);
      2. favourable levels in ascending order up to the favourable extreme:
         TP1 partial (-> break-even), 2R exit, native TP;
      3. trailing update at the favourable extreme (production formula);
      4. a stop tightened during this bar exits only if the CLOSE is beyond it;
      5. mark at the close.
    Returns legs (ts, weight, fill, reason), marks (close_ts, close), exit
    metadata. ``None`` when the entry cannot be simulated.
    """
    if start_idx >= len(bars):
        return None
    ts_of = ts_of or (lambda b: int(b["ts"]))
    d = str(direction).upper()
    s = 1.0 if d == "LONG" else -1.0
    first = bars[start_idx]
    fill = _slip(float(first["o"]), d, slippage_rate, is_entry=True)
    if fill <= 0 or signal_entry <= 0:
        return None
    delta = fill - float(signal_entry)
    local_entry = fill
    local_sl = float(signal_sl) + delta
    local_tp = float(signal_tp) + delta
    native_sl = local_sl if policy.shift_native_stops else float(signal_sl)
    native_tp = local_tp if policy.shift_native_stops else float(signal_tp)
    if s * (fill - native_sl) <= 0:
        # Fill already beyond the native stop: KuCoin triggers immediately.
        native_sl_breached_at_entry = True
    else:
        native_sl_breached_at_entry = False

    qty = 1.0
    peak_pnl = 0.0
    tp1_hit = False
    tp1_ts = None
    trailing_sl = local_sl
    legs: list[tuple[int, float, float, str]] = []
    marks: list[tuple[int, float]] = []
    exit_reason = None
    exit_ts = None
    stop_kind = "NATIVE_SL"

    def fav(x: float) -> float:
        return s * x

    def close_all(ts: int, price: float, reason: str):
        nonlocal qty, exit_reason, exit_ts
        legs.append((ts, qty, _slip(price, d, slippage_rate, is_entry=False), reason))
        qty = 0.0
        exit_reason, exit_ts = reason, ts

    last_idx = min(len(bars) - 1, start_idx + int(policy.research_max_hold_bars) - 1)
    for j in range(start_idx, last_idx + 1):
        b = bars[j]
        o, h, l, c = float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"])
        close_ts = ts_of(b) + FIFTEEN_MIN_MS
        adverse = l if s > 0 else h
        favourable = h if s > 0 else l

        # 1. native stop in force at bar start
        if native_sl_breached_at_entry and j == start_idx:
            close_all(close_ts, fill, "NATIVE_SL_AT_ENTRY")
            break
        if fav(adverse) <= fav(native_sl):
            trigger = o if fav(o) <= fav(native_sl) else native_sl
            close_all(close_ts, trigger, stop_kind)
            break

        # 2. favourable levels in ascending order
        stop_tightened = False
        while qty > 0:
            events = []
            if policy.enable_partial and not tp1_hit:
                dist = abs(local_entry - local_sl)
                if dist > 0:
                    lvl = local_entry + s * (dist + local_entry * policy.tp1_funding_buffer)
                    events.append((fav(lvl), "TP1", lvl))
            if policy.enable_rr_double:
                dist = abs(local_entry - local_sl)
                if dist > 0:
                    lvl = local_entry + s * policy.rr_double_multiple * dist
                    events.append((fav(lvl), "RR_DOUBLE", lvl))
            events.append((fav(native_tp), "NATIVE_TP", native_tp))
            reachable = [e for e in events if e[0] <= fav(favourable)]
            if not reachable:
                break
            _, kind, lvl = min(reachable, key=lambda e: (e[0], e[1]))
            trigger = o if fav(o) >= fav(lvl) else lvl
            if kind == "NATIVE_TP":
                close_all(close_ts, trigger, "NATIVE_TP")
                break
            if kind == "RR_DOUBLE":
                close_all(close_ts, trigger, "RR_DOUBLE")
                break
            # TP1 partial at market; peak recorded with the pre-partial qty
            peak_pnl = max(peak_pnl, s * (trigger - local_entry) * qty)
            part = min(qty, policy.partial_fraction)
            legs.append((close_ts, part, _slip(trigger, d, slippage_rate, is_entry=False), "PARTIAL_TP1"))
            qty -= part
            tp1_hit, tp1_ts = True, close_ts
            local_sl = local_entry
            trailing_sl = local_entry
            native_sl = local_entry
            stop_kind = "BREAK_EVEN_SL"
            stop_tightened = True
            if qty <= 1e-12:
                qty = 0.0
                exit_reason, exit_ts = "PARTIAL_TP1_FULL", close_ts
                break
        if qty <= 0:
            break

        # 3. trailing update at the favourable extreme (production formula)
        pnl = s * (favourable - local_entry) * qty
        peak_pnl = max(peak_pnl, pnl)
        if policy.enable_trailing and pnl > 0 and local_tp != local_entry:
            target = abs(local_tp - local_entry)
            if target > 0 and pnl >= target * policy.trailing_trigger * qty:
                excursion = peak_pnl / qty
                retained = excursion * (1.0 - max(0.0, min(1.0, policy.trailing_lock)))
                new_sl = local_entry + s * retained
                new_sl = max(new_sl, local_sl) if s > 0 else min(new_sl, local_sl)
                valid_side = fav(new_sl) < fav(favourable)   # native_stop_repair side check
                if valid_side and fav(new_sl) > fav(trailing_sl):
                    trailing_sl = new_sl
                    local_sl = new_sl
                    native_sl = new_sl
                    stop_kind = "TRAILING_SL"
                    stop_tightened = True
                    if policy.enable_rr_double:
                        dist = abs(local_entry - local_sl)
                        if dist > 0 and s * (favourable - local_entry) >= policy.rr_double_multiple * dist:
                            lvl = local_entry + s * policy.rr_double_multiple * dist
                            close_all(close_ts, lvl, "RR_DOUBLE")
                            break

        # 4. tightened stop: certain crossing only
        if stop_tightened and fav(c) <= fav(native_sl):
            close_all(close_ts, native_sl, stop_kind + "_SAME_BAR")
            break

        marks.append((close_ts, c))

    outcome_status = RESOLVED
    censor_ts = None
    last_mark = None
    if qty > 0:
        # No production exit happened inside the available history (or the
        # research cap). Production has no forced close: the position is
        # RIGHT-CENSORED. No artificial exit leg, fee or realized PnL is made.
        last = bars[last_idx]
        censor_ts = ts_of(last) + FIFTEEN_MIN_MS
        last_mark = float(last["c"])
        outcome_status = (RIGHT_CENSORED_DATA_END if last_idx == len(bars) - 1
                          else RIGHT_CENSORED_RESEARCH_LIMIT)
        exit_ts = None
        exit_reason = None

    return {
        "fill": fill,
        "outcome_status": outcome_status,
        "native_sl_initial": float(signal_sl) if not policy.shift_native_stops else float(signal_sl) + delta,
        "native_tp": native_tp,
        "exchange_native_geometry": {
            "sl_initial": float(signal_sl) + (delta if policy.shift_native_stops else 0.0),
            "tp": native_tp, "sl_at_end": native_sl},
        "local_position_geometry": {"entry": local_entry, "sl_initial": float(signal_sl) + delta,
                                    "tp": local_tp, "sl_at_end": local_sl},
        "legs": legs,
        "marks": marks,
        "exit_ts": exit_ts,
        "exit_reason": exit_reason,
        "tp1_ts": tp1_ts,
        "censored": None if outcome_status == RESOLVED else outcome_status,
        "censor_ts": censor_ts,
        "open_qty_at_censor": qty if outcome_status != RESOLVED else 0.0,
        "native_sl_at_censor": native_sl if outcome_status != RESOLVED else None,
        "last_mark": last_mark,
        "entry_ts": ts_of(first),
    }


def legs_net_r(sim: dict, *, direction: str, fee_rate: float, funding_events: Sequence[dict],
               price_at_ts, planned_risk_fraction: float, slippage_rate: float = 0.0) -> dict:
    """Gross / fees / funding in R. R is the planned stop risk at sizing time
    (|signal entry - signal stop| / signal entry).

    RESOLVED outcomes get a realized ``r``. RIGHT-CENSORED outcomes get
    ``r = None`` (never a completed trade) plus diagnostics only:
    ``marked_r`` (open remainder marked at the last close, no exit fee) and
    reasonable bounds with the open remainder closed at the CURRENT native
    stop (worst) or the native TP (best), with slippage and exit fee.
    """
    from bot.kucoin_execution_model import fee_return_fraction, funding_return_fraction
    fill = sim["fill"]
    s = 1.0 if str(direction).upper() == "LONG" else -1.0
    rf = float(planned_risk_fraction)
    if not rf > 0:
        return {"r": None, "outcome_status": sim.get("outcome_status", RESOLVED)}
    end_ts = sim["exit_ts"] if sim.get("outcome_status", RESOLVED) == RESOLVED else sim["censor_ts"]
    funding, n = funding_return_fraction(
        list(funding_events), direction, sim["entry_ts"], end_ts, fill,
        price_at_ts=price_at_ts, partial_after_ts_ms=sim.get("tp1_ts"),
    )
    realized_gross = sum(s * (p - fill) / fill * w for _, w, p, _ in sim["legs"])
    realized_fees = fee_return_fraction(fill, [(p, w) for _, w, p, _ in sim["legs"]], fee_rate)
    if sim.get("outcome_status", RESOLVED) == RESOLVED:
        return {"r": (realized_gross - realized_fees + funding) / rf, "gross_r": realized_gross / rf,
                "fees_r": -realized_fees / rf, "funding_r": funding / rf, "funding_events": n,
                "outcome_status": RESOLVED}
    q = float(sim["open_qty_at_censor"])
    marked = realized_gross + s * (float(sim["last_mark"]) - fill) / fill * q

    def _bound(level):
        px = _slip(float(level), direction, slippage_rate, is_entry=False)
        gross = realized_gross + s * (px - fill) / fill * q
        fees = realized_fees + fee_rate * (px / fill) * q
        return (gross - fees + funding) / rf

    return {"r": None, "outcome_status": sim["outcome_status"],
            "marked_r": (marked - realized_fees + funding) / rf,
            "bound_worst_r": _bound(sim["native_sl_at_censor"]),
            "bound_best_r": _bound(sim["native_tp"]),
            "realized_part_r": (realized_gross - realized_fees) / rf,
            "funding_r": funding / rf, "funding_events": n}


# ── sizing parity ───────────────────────────────────────────────────────────
def instrument_from_public_contract(contract: dict) -> dict:
    """Production-shaped instrument dict (KuCoinClient.load_instruments +
    kucoin_contract_risk_hardening public MMR reference)."""
    lot_size = float(contract.get("lotSize", 1))
    mult = float(contract.get("multiplier", 0.001))
    info = {
        "minQty": float(contract.get("minQty", lot_size)),
        "lotSize": lot_size,
        "qtyStep": lot_size,
        "tickSize": float(contract.get("tickSize", 0.01)),
        "multiplier": mult,
        "maxLeverage": float(contract.get("maxLeverage", 0) or 0),
        "minBaseQty": float(contract.get("minQty", lot_size)) * mult,
        "minNotional": float(contract.get("minNotional", 0) or 0),
        "kucoinSymbol": contract.get("symbol"),
    }
    try:
        m = float(contract.get("maintainMargin"))
    except (TypeError, ValueError):
        m = float("nan")   # no public MMR: sizing uses risk_policy's conservative fallback
    if math.isfinite(m) and 0 < m < 1:
        info["contractMaintainMarginReference"] = m
    return info


def production_cost_fraction(*, taker_fee: float, expected_slippage_pct: float,
                             modeled_round_trip_pct: float) -> float:
    """professional_risk_adapter.conservative_cost_fraction with explicit inputs."""
    from bot import risk_policy as rp
    return rp.round_trip_cost_fraction(
        taker_fee_per_side=float(taker_fee),
        expected_slippage_pct=float(expected_slippage_pct),
        modeled_round_trip_fraction=float(modeled_round_trip_pct) / 100.0,
    )


def replay_size_entry(*, policy, info: dict, equity: float, available: float,
                      signal_entry: float, signal_sl: float, direction: str,
                      cost_fraction: float, risk_pct: float, max_adverse_entry_drift: float,
                      open_risks=()) -> dict:
    """Mirror of final_sizing_invariants.size_pilot_entry for identical inputs.

    Production sizes at the SIGNAL entry (``sig.entry``) and SIGNAL stop, not
    at the later fill; the drift allowance covers the fill the pre-dispatch
    guard still accepts. Both production quantities (canonical and the
    RiskManagerV3 adapter) are ``risk_policy.size_new_entry`` of the same
    snapshot, so the minimum equals the canonical quantity.
    """
    from bot import risk_policy as rp
    from bot.professional_risk_adapter import _maintenance_margin_rate
    from bot.final_loss_budget import validate
    rules = rp.QuantityRules.from_instrument(info)
    dec = rp.size_new_entry(
        policy=policy, equity=float(equity), available=float(available),
        entry=float(signal_entry), stop=float(signal_sl), direction=str(direction).upper(),
        rules=rules, cost_fraction=float(cost_fraction), risk_pct=float(risk_pct),
        maintenance_margin_rate=_maintenance_margin_rate(info), open_risks=list(open_risks),
        max_adverse_entry_drift=float(max_adverse_entry_drift),
    )
    if not dec.allowed:
        return {"qty": 0.0, "reason": f"canonical_{dec.reason}", "decision": dec}
    final_d = rules.floor_base(float(dec.qty))
    if final_d <= 0 or final_d < rules.minimum_base(float(signal_entry)):
        return {"qty": 0.0, "reason": "exchange_minimum_exceeds_safe_quantity", "decision": dec}
    qty = float(final_d)
    try:
        validate(qty, float(signal_entry), float(signal_sl), str(direction).upper(),
                 float(policy.leverage), float(cost_fraction), equity=float(equity),
                 risk_pct=float(risk_pct))
    except Exception as exc:  # LossBudgetExceeded and malformed inputs block
        return {"qty": 0.0, "reason": f"loss_budget_{type(exc).__name__}", "decision": dec}
    margin = qty * float(signal_entry) / float(policy.leverage)
    cap = float(available) * min(policy.max_margin_pct, policy.operator_margin_cap_pct)
    if not math.isfinite(margin) or margin > cap * (1 + 1e-9):
        return {"qty": 0.0, "reason": "margin_cap_exceeded", "decision": dec}
    return {"qty": qty, "reason": "ok", "decision": dec, "margin": margin,
            "projected_loss_at_stop": dec.projected_loss_at_stop}
