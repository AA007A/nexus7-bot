"""Strict authority hierarchy (AI can only say "no" or "this opportunity"):

    AI decision
      -> deterministic strategy / geometry validation
      -> halt controller + operator kill switch
      -> drawdown hard gate            (risk_policy.drawdown_entry_decision)
      -> daily stop                    (risk_policy.effective_daily_stop_limit)
      -> canonical sizing: per-trade risk, margin, operator cap, liquidation
         safety, portfolio budget, exchange lot (risk_policy.size_new_entry)
      -> execution authority (execution_mode)

Invariants (tested):
  * any deterministic veto beats any AI output;
  * size comes ONLY from the risk engine — AI confidence is never an input,
    so it can never resize a position upward;
  * the order intent carries a deterministic client order id derived from the
    decision id, so a duplicated decision cannot create duplicated exposure.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from bot import risk_policy as rp
from bot.ai.decision import AIDecision, LONG, SHORT


@dataclass(frozen=True)
class Geometry:
    entry: float
    stop: float
    tp1: float
    tp2: float


@dataclass
class RiskContext:
    policy: rp.RiskPolicy
    equity: float
    available: float
    drawdown: float
    daily_realized_loss: float
    day_start_balance: float
    rules: rp.QuantityRules
    cost_fraction: float
    maintenance_margin_rate: float | None = None
    open_risks: tuple = ()
    kill_switch: bool = False


@dataclass
class OrderIntent:
    client_oid: str
    decision_id: str
    symbol: str
    side: str
    qty: float
    entry: float
    stop: float
    tp1: float
    tp2: float
    sizing: str


@dataclass
class ChainResult:
    approved: bool
    stage: str
    vetoes: list = field(default_factory=list)
    intent: OrderIntent | None = None
    ai: AIDecision | None = None


def client_oid(decision_id: str) -> str:
    """Deterministic, exchange-safe idempotency identity for one decision."""
    return "bgxai" + hashlib.sha256(decision_id.encode()).hexdigest()[:27]


def validate_geometry(side: str, g: Geometry, min_rr: float) -> list[str]:
    out = []
    vals = (g.entry, g.stop, g.tp1, g.tp2)
    if any((not isinstance(v, (int, float))) or v != v or v <= 0 for v in vals):
        return ["GEOMETRY_INVALID_NUMBERS"]
    if side == LONG and not (g.stop < g.entry < g.tp1 <= g.tp2):
        out.append("GEOMETRY_ORDER_INVALID_LONG")
    if side == SHORT and not (g.stop > g.entry > g.tp1 >= g.tp2):
        out.append("GEOMETRY_ORDER_INVALID_SHORT")
    risk = abs(g.entry - g.stop)
    if risk <= 0 or abs(g.tp2 - g.entry) / risk < min_rr:
        out.append("GEOMETRY_RR_BELOW_MIN")
    return out


def run(ai: AIDecision, geometry: Geometry, ctx: RiskContext, *, halted: bool,
        halt_reasons=()) -> ChainResult:
    if not ai.is_trade:
        return ChainResult(False, "AI", list(ai.vetoes) or ["AI_ABSTAIN"], ai=ai)
    side = ai.side
    v = validate_geometry(side, geometry, ctx.policy.min_rr_ratio)
    if v:
        return ChainResult(False, "STRATEGY_VALIDATION", v, ai=ai)
    if halted or ctx.kill_switch:
        return ChainResult(False, "HALT", list(halt_reasons) or ["OPERATOR_KILL_SWITCH"], ai=ai)
    dd = rp.drawdown_entry_decision(ctx.drawdown, ctx.policy.max_drawdown)
    if not dd.can_open:
        return ChainResult(False, "DRAWDOWN", [f"DRAWDOWN_{dd.reason.upper()}"], ai=ai)
    try:
        limit = rp.effective_daily_stop_limit(ctx.day_start_balance, ctx.policy)
    except rp.RiskPolicyError as exc:
        return ChainResult(False, "DAILY_STOP", [f"DAILY_STOP_INVALID:{exc}"], ai=ai)
    if ctx.daily_realized_loss >= limit.limit:
        return ChainResult(False, "DAILY_STOP", ["DAILY_STOP_REACHED"], ai=ai)
    sizing = rp.size_new_entry(
        policy=ctx.policy, equity=ctx.equity, available=ctx.available, entry=geometry.entry,
        stop=geometry.stop, direction=side, rules=ctx.rules, cost_fraction=ctx.cost_fraction,
        maintenance_margin_rate=ctx.maintenance_margin_rate, open_risks=ctx.open_risks)
    if not sizing.allowed:
        return ChainResult(False, "RISK_SIZING",
                           [f"RISK_{sizing.binding_constraint.upper()}:{sizing.reason}"], ai=ai)
    intent = OrderIntent(client_oid(ai.decision_id), ai.decision_id, ai.symbol, side, sizing.qty,
                         geometry.entry, geometry.stop, geometry.tp1, geometry.tp2,
                         sizing.log_fields())
    return ChainResult(True, "EXECUTION_AUTHORITY", [], intent=intent, ai=ai)
