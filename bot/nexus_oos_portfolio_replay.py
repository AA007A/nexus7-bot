"""PORTFOLIO_EXECUTION_REPLAY: event-driven replay of NEXUS-approved candidates.

Research only: no network, no exchange mutation, no runtime mutation.

The candidate replay answers "does the signal-selection layer have edge?".
This layer answers "would the executable strategy, under its position and risk
constraints, have made money?". It consumes candidate rows (each carrying its
realized trade path from the candidate replay) and walks them chronologically:

* entries are admitted only if the historical bot could have taken them:
  free position slot (MAX_POSITIONS), no open position on the same symbol,
  daily stop not triggered for that UTC day, drawdown hard gate not active,
  and ``risk_policy.size_new_entry`` returns a positive quantity from the
  CURRENT marked equity and FREE collateral;
* collateral is locked at entry and released only at the trade's final exit;
* realized PnL (gross legs, fees, slippage, funding) is booked at exit; open
  positions are marked to the last closed 15m candle for equity, daily-stop
  and drawdown checks;
* same-timestamp candidates are processed in a fixed documented order:
  (decision_ts, symbol) ascending. This ordering is not optimized for profit.

Documented approximations (reported in the artifact):
* circuit breakers are evaluated at candidate and exit events, not every 5s;
* collateral from a TP1 partial is released only at final exit (conservative);
* contract lot/multiplier use current public KuCoin metadata when available,
  otherwise a fine-lot fallback (``contract_metadata`` in the artifact);
* discretionary exits (trailing, CHoCH, regime/signal invalidation, min-hold)
  are not modeled, as in the candidate replay.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timezone
import math
from typing import Mapping, Sequence

from bot import risk_policy as rp
from bot import nexus_oos_inference as inf

FINE_LOT_RULES = rp.QuantityRules(Decimal("0.000001"), Decimal(1), Decimal(1), Decimal(0))


@dataclass(frozen=True)
class PortfolioConfig:
    starting_equity: float = 1000.0
    research_max_drawdown_limit: float = 0.25   # gate limit for the replay's equity DD


def _day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _mark(position: dict, ts: int) -> float:
    """Unrealized PnL (quote) at the last closed candle <= ts."""
    price = position["entry_fill"]
    for t, close in position["path"]:
        if t <= ts:
            price = close
        else:
            break
    side = 1.0 if position["direction"] == "LONG" else -1.0
    return side * (price - position["entry_fill"]) / position["entry_fill"] * position["notional"]


def run_portfolio(rows: Sequence[dict], policy: rp.RiskPolicy, *,
                  config: PortfolioConfig = PortfolioConfig(),
                  contract_rules: Mapping[str, rp.QuantityRules] | None = None,
                  mmr: Mapping[str, float] | None = None) -> dict:
    candidates = sorted((r for r in rows if r.get("approved")), key=lambda r: (int(r["ts"]), str(r["symbol"])))
    cash = float(config.starting_equity)          # realized equity
    peak = cash
    open_pos: list[dict] = []
    trades: list[dict] = []
    skipped = defaultdict(int)
    curve: list[tuple[int, float]] = []
    day_start_equity: dict[str, float] = {}
    day_stopped: set[str] = set()
    dd_blocked_events = 0
    exposure_ms = 0
    last_event_ts = None
    margin_time = 0.0
    max_concurrent = 0

    def equity_at(ts):
        return cash + sum(_mark(p, ts) for p in open_pos)

    def advance(ts):
        nonlocal exposure_ms, last_event_ts, margin_time
        if last_event_ts is not None and ts > last_event_ts:
            dt = ts - last_event_ts
            if open_pos:
                exposure_ms += dt
            eq = max(equity_at(last_event_ts), 1e-12)
            margin_time += dt * sum(p["margin"] for p in open_pos) / eq
        last_event_ts = ts if last_event_ts is None else max(last_event_ts, ts)

    def close_until(ts):
        nonlocal cash, peak
        while True:
            due = [p for p in open_pos if p["exit_ts"] <= ts]
            if not due:
                return
            p = min(due, key=lambda x: (x["exit_ts"], x["symbol"]))
            advance(p["exit_ts"])
            cash += p["pnl"]
            open_pos.remove(p)
            trades.append(p)
            eq = equity_at(p["exit_ts"])
            peak = max(peak, eq)
            curve.append((p["exit_ts"], eq))

    for row in candidates:
        ts = int(row["ts"])
        close_until(ts)
        advance(ts)
        eq = equity_at(ts)
        peak = max(peak, eq)
        curve.append((ts, eq))
        day = _day(ts)
        day_start_equity.setdefault(day, eq)

        # Daily stop (stricter of pct / absolute on the day's starting equity).
        if day not in day_stopped:
            try:
                limit = rp.effective_daily_stop_limit(day_start_equity[day], policy).limit
            except rp.RiskPolicyError:
                limit = 0.0
            if eq - day_start_equity[day] <= -limit:
                day_stopped.add(day)
        if day in day_stopped:
            skipped["daily_stop"] += 1
            continue
        drawdown = (peak - eq) / peak if peak > 0 else 1.0
        if not rp.drawdown_entry_decision(drawdown, policy.max_drawdown).can_open:
            skipped["drawdown_hard_gate"] += 1
            dd_blocked_events += 1
            continue
        if len(open_pos) >= policy.max_positions:
            skipped["position_limit"] += 1
            continue
        if any(p["symbol"] == row["symbol"] for p in open_pos):
            skipped["same_symbol_open"] += 1
            continue

        used_margin = sum(p["margin"] for p in open_pos)
        available = eq - used_margin
        rules = (contract_rules or {}).get(row["symbol"], FINE_LOT_RULES)
        cost_fraction = max(0.0, -(row.get("fees_r") or 0.0) * row["risk_fraction"]) \
            + max(0.0, -(row.get("slippage_r") or 0.0) * row["risk_fraction"])
        open_risks = [rp.OpenRisk(p["symbol"], p["risk_at_stop"]) for p in open_pos]
        decision = rp.size_new_entry(
            policy=policy, equity=eq, available=available, entry=row["entry_fill"],
            stop=row["stop"], direction=row["direction"], rules=rules,
            cost_fraction=cost_fraction, maintenance_margin_rate=(mmr or {}).get(row["symbol"]),
            open_risks=open_risks,
        )
        if not decision.allowed:
            skipped[f"risk_capital:{decision.binding_constraint}"] += 1
            continue
        notional = decision.qty * row["entry_fill"]
        pnl = float(row["r"]) * row["risk_fraction"] * notional
        open_pos.append({
            "symbol": row["symbol"], "direction": row["direction"], "entry_ts": ts,
            "exit_ts": int(row["outcome_end_ts"]), "entry_fill": row["entry_fill"],
            "notional": notional, "margin": decision.required_margin,
            "risk_at_stop": decision.projected_loss_at_stop, "pnl": pnl, "r": float(row["r"]),
            "fees": (row.get("fees_r") or 0.0) * row["risk_fraction"] * notional,
            "slippage": (row.get("slippage_r") or 0.0) * row["risk_fraction"] * notional,
            "funding": (row.get("funding_r") or 0.0) * row["risk_fraction"] * notional,
            "path": row.get("_path") or [], "month": row.get("month"),
            "production_regime": row.get("production_regime"),
            "research_regime": row.get("research_regime"),
        })
        max_concurrent = max(max_concurrent, len(open_pos))

    close_until(2 ** 62)
    return _report(trades, skipped, curve, config, policy, exposure_ms, margin_time,
                   max_concurrent, candidates, contract_rules)


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
    return {k: {"trades": len(v), "net_pnl": sum(t["pnl"] for t in v),
                "avg_r": sum(t["r"] for t in v) / len(v)} for k, v in sorted(groups.items())}


def _report(trades, skipped, curve, config, policy, exposure_ms, margin_time, max_concurrent,
            candidates, contract_rules):
    start = float(config.starting_equity)
    end = start + sum(t["pnl"] for t in trades)
    rs = [t["r"] for t in sorted(trades, key=lambda t: t["exit_ts"])]
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [-t["pnl"] for t in trades if t["pnl"] < 0]
    win_r = [r for r in rs if r > 0.02]
    loss_r = [r for r in rs if r < -0.02]
    loss_streak = cur = 0
    for r in rs:
        cur = cur + 1 if r < -0.02 else 0
        loss_streak = max(loss_streak, cur)
    mdd, mdd_dur = _drawdown(curve)
    span = (max(t["exit_ts"] for t in trades) - min(t["entry_ts"] for t in trades)) if trades else 0
    trade_rows = [{"ts": t["entry_ts"], "r": t["r"], "symbol": t["symbol"]} for t in trades]
    robust = inf.dependence_aware_mean(trade_rows) if len(trade_rows) >= 2 else {}
    return {
        "layer": "PORTFOLIO_EXECUTION_REPLAY",
        "policy": {"leverage": policy.leverage, "max_risk_pct": policy.max_risk_pct,
                   "max_margin_pct": policy.max_margin_pct,
                   "operator_margin_cap_pct": policy.operator_margin_cap_pct,
                   "max_drawdown": policy.max_drawdown, "max_positions": policy.max_positions,
                   "daily_stop_loss_pct": policy.daily_stop_loss_pct,
                   "daily_stop_loss_abs": policy.daily_stop_loss_abs},
        "same_timestamp_ordering": "(decision_ts, symbol) ascending; not profit-optimized",
        "contract_metadata": ("KUCOIN_PUBLIC_CURRENT" if contract_rules else "FINE_LOT_FALLBACK"),
        "approximations": [
            "circuit breakers evaluated at candidate/exit events, not every 5s",
            "TP1 partial collateral released at final exit (conservative)",
            "discretionary exits not modeled (trailing, CHoCH, regime/signal invalidation, min-hold)",
            "pre-trade score gate (score.py) not modeled",
        ],
        "starting_equity": start,
        "ending_equity": end,
        "net_return": (end - start) / start,
        "approved_candidates": len(candidates),
        "total_trades": len(trades),
        "skipped": dict(sorted(skipped.items())),
        "skipped_position_limit": skipped.get("position_limit", 0) + skipped.get("same_symbol_open", 0),
        "skipped_capital_risk": sum(v for k, v in skipped.items() if k.startswith("risk_capital")),
        "blocked_daily_stop": skipped.get("daily_stop", 0),
        "blocked_drawdown": skipped.get("drawdown_hard_gate", 0),
        "total_fees": sum(t["fees"] for t in trades),
        "total_slippage": sum(t["slippage"] for t in trades),
        "total_funding": sum(t["funding"] for t in trades),
        "portfolio_max_drawdown": mdd,
        "portfolio_max_drawdown_duration_hours": mdd_dur / inf.HOUR_MS,
        "research_max_drawdown_limit": config.research_max_drawdown_limit,
        "profit_factor": (sum(wins) / sum(losses)) if losses else None,
        "net_expectancy_r": (sum(rs) / len(rs)) if rs else None,
        "net_expectancy_quote": ((end - start) / len(trades)) if trades else None,
        "avg_r": (sum(rs) / len(rs)) if rs else None,
        "win_rate": (len(win_r) / len(rs)) if rs else None,
        "avg_winner_r": (sum(win_r) / len(win_r)) if win_r else None,
        "avg_loser_r": (sum(loss_r) / len(loss_r)) if loss_r else None,
        "longest_losing_streak": loss_streak,
        "exposure_time_fraction": (exposure_ms / span) if span > 0 else None,
        "avg_capital_utilization": (margin_time / span) if span > 0 else None,
        "max_concurrent_positions": max_concurrent,
        "robustness": robust,
        "by_symbol": _group(trades, "symbol"),
        "by_production_regime": _group(trades, "production_regime"),
        "by_research_regime": _group(trades, "research_regime"),
        "by_direction": _group(trades, "direction"),
        "by_month": _group(trades, "month"),
        "contributing_symbols": sum(1 for v in _group(trades, "symbol").values() if v["net_pnl"] > 0),
    }


def contract_rules_from_public(contracts: Sequence[dict], symbols: Sequence[str]):
    """Map KuCoin public active contracts to QuantityRules and maintenance rates."""
    alias = {"XBT": "BTC"}
    rules, mmr = {}, {}
    for c in contracts or []:
        if not isinstance(c, dict) or not str(c.get("symbol", "")).endswith("USDTM"):
            continue
        base = alias.get(str(c.get("baseCurrency", "")), str(c.get("baseCurrency", "")))
        std = f"{base}USDT"
        if std not in symbols:
            continue
        try:
            multiplier = Decimal(str(c["multiplier"]))
            lot = Decimal(str(c.get("lotSize", 1)))
            if multiplier <= 0 or lot <= 0:
                continue
            rules[std] = rp.QuantityRules(multiplier, lot, lot, Decimal(0))
            m = float(c.get("maintainMargin", 0) or 0)
            if math.isfinite(m) and 0 < m < 1:
                mmr[std] = m
        except (KeyError, ArithmeticError, ValueError, TypeError):
            continue
    return rules, mmr
