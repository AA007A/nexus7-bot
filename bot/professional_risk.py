"""Professional risk primitives for NEXUS-7.

Pure, side-effect-free building blocks used to migrate the engine from
buying-power sizing to explicit risk-budget sizing. No exchange mutation lives
in this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
import math
from typing import Mapping, Any


@dataclass(frozen=True)
class CapitalState:
    """Separate accounting concepts that must never be conflated."""

    equity: float
    available_collateral: float
    position_margin: float = 0.0
    order_margin: float = 0.0
    unrealized_pnl: float = 0.0

    def validate(self) -> "CapitalState":
        vals = (
            self.equity,
            self.available_collateral,
            self.position_margin,
            self.order_margin,
            self.unrealized_pnl,
        )
        if any(not math.isfinite(float(v)) for v in vals):
            raise ValueError("capital state contains non-finite value")
        if self.equity < 0 or self.available_collateral < 0:
            raise ValueError("equity/available collateral cannot be negative")
        if self.position_margin < 0 or self.order_margin < 0:
            raise ValueError("margin components cannot be negative")
        return self

    @property
    def committed_margin(self) -> float:
        return self.position_margin + self.order_margin


@dataclass(frozen=True)
class StopRiskSizingResult:
    qty: float
    notional: float
    risk_budget: float
    projected_stop_loss: float
    stop_distance_pct: float
    required_margin: float
    binding_constraint: str


def _floor_step(value: float, step: float) -> float:
    if value <= 0 or step <= 0:
        return 0.0
    d_value = Decimal(str(value))
    d_step = Decimal(str(step))
    steps = int((d_value / d_step).to_integral_value(rounding=ROUND_FLOOR))
    return float(Decimal(steps) * d_step)


def stop_risk_size(
    *,
    capital: CapitalState,
    entry: float,
    stop: float,
    risk_pct: float,
    leverage: float,
    qty_step: float,
    min_qty: float,
    max_margin_pct: float,
    fee_rate_per_side: float = 0.0,
    expected_slippage_pct: float = 0.0,
) -> StopRiskSizingResult:
    """Size by planned stop loss, then clamp by collateral/margin.

    Core invariant:
        risk_budget = equity * risk_pct
        qty ~= risk_budget / effective_loss_per_base_unit

    Effective loss includes price distance to the stop, round-trip fees and a
    conservative slippage allowance. Leverage constrains required collateral;
    it does not define the market-risk budget.
    """
    capital.validate()
    numeric = (entry, stop, risk_pct, leverage, qty_step, min_qty, max_margin_pct)
    if any(not math.isfinite(float(v)) for v in numeric):
        raise ValueError("non-finite sizing input")
    if entry <= 0 or stop <= 0 or entry == stop:
        raise ValueError("entry and stop must be positive and different")
    if not 0 < risk_pct <= 1:
        raise ValueError("risk_pct must be in (0,1]")
    if leverage <= 0:
        raise ValueError("leverage must be positive")
    if qty_step <= 0 or min_qty <= 0:
        raise ValueError("quantity rules must be positive")
    if not 0 < max_margin_pct <= 1:
        raise ValueError("max_margin_pct must be in (0,1]")
    if fee_rate_per_side < 0 or expected_slippage_pct < 0:
        raise ValueError("fees/slippage cannot be negative")

    stop_distance = abs(entry - stop)
    stop_distance_pct = stop_distance / entry
    risk_budget = capital.equity * risk_pct

    # Conservative projected loss per base unit at stop.
    fee_loss_per_unit = entry * fee_rate_per_side * 2.0
    slippage_loss_per_unit = entry * expected_slippage_pct
    effective_loss_per_unit = stop_distance + fee_loss_per_unit + slippage_loss_per_unit
    if effective_loss_per_unit <= 0:
        raise ValueError("effective loss per unit must be positive")

    qty_by_risk = risk_budget / effective_loss_per_unit

    collateral_cap = capital.available_collateral * max_margin_pct
    notional_cap = collateral_cap * leverage
    qty_by_margin = notional_cap / entry

    raw_qty = min(qty_by_risk, qty_by_margin)
    binding = "RISK_BUDGET" if qty_by_risk <= qty_by_margin else "AVAILABLE_COLLATERAL"
    qty = _floor_step(raw_qty, qty_step)

    if qty < min_qty:
        return StopRiskSizingResult(
            qty=0.0,
            notional=0.0,
            risk_budget=risk_budget,
            projected_stop_loss=0.0,
            stop_distance_pct=stop_distance_pct,
            required_margin=0.0,
            binding_constraint="MINIMUM_ORDER",
        )

    notional = qty * entry
    required_margin = notional / leverage
    projected_stop_loss = qty * effective_loss_per_unit

    # Rounding must never inflate loss above the configured budget.
    if projected_stop_loss > risk_budget * 1.000001:
        raise AssertionError("rounded quantity exceeds risk budget")
    if required_margin > collateral_cap * 1.000001:
        raise AssertionError("rounded quantity exceeds collateral cap")

    return StopRiskSizingResult(
        qty=qty,
        notional=notional,
        risk_budget=risk_budget,
        projected_stop_loss=projected_stop_loss,
        stop_distance_pct=stop_distance_pct,
        required_margin=required_margin,
        binding_constraint=binding,
    )


def capital_state_from_account_overview(data: Mapping[str, Any]) -> CapitalState:
    """Normalize KuCoin account-overview semantics without losing distinctions."""
    if not isinstance(data, Mapping):
        raise ValueError("account overview must be a mapping")
    def f(key: str) -> float:
        value = data.get(key, 0.0)
        if isinstance(value, bool):
            raise ValueError(f"invalid boolean account field: {key}")
        return float(value or 0.0)

    equity = f("accountEquity") or f("equity") or f("marginBalance")
    state = CapitalState(
        equity=equity,
        available_collateral=f("availableBalance"),
        position_margin=f("positionMargin"),
        order_margin=f("orderMargin"),
        unrealized_pnl=f("unrealisedPNL") or f("unrealisedPnl"),
    )
    return state.validate()
