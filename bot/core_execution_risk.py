"""Core risk/execution coordinator for NEXUS-7.

This module composes the professional risk primitives and fail-closed
pre-dispatch checks into one auditable decision object. It contains no exchange
mutation and does not authorize LIVE trading by itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from bot.professional_risk import (
    CapitalState,
    StopRiskSizingResult,
    capital_state_from_account_overview,
    stop_risk_size,
)
from bot.pre_dispatch_guard import (
    MicrostructureLimits,
    PreDispatchResult,
    evaluate_microstructure,
    recheck_exchange_exposure,
)


@dataclass(frozen=True)
class ExecutionRiskPlan:
    capital: CapitalState
    sizing: StopRiskSizingResult
    microstructure: PreDispatchResult

    @property
    def allowed(self) -> bool:
        return self.sizing.qty > 0 and self.microstructure.allowed


def build_execution_risk_plan(
    *,
    account_overview: Mapping[str, Any],
    entry: float,
    stop: float,
    side: str,
    risk_pct: float,
    leverage: float,
    qty_step: float,
    min_qty: float,
    max_margin_pct: float,
    fee_rate_per_side: float,
    expected_slippage_pct: float,
    ticker: Mapping[str, Any],
    orderbook: Mapping[str, Any] | None,
    microstructure_limits: MicrostructureLimits = MicrostructureLimits(),
) -> ExecutionRiskPlan:
    """Build a stop-risk-sized plan from explicit account semantics.

    Equity determines the loss budget. Available collateral constrains margin.
    Position/order margin remain explicit accounting fields and are never
    substituted for either concept.
    """
    capital = capital_state_from_account_overview(account_overview)
    sizing = stop_risk_size(
        capital=capital,
        entry=entry,
        stop=stop,
        risk_pct=risk_pct,
        leverage=leverage,
        qty_step=qty_step,
        min_qty=min_qty,
        max_margin_pct=max_margin_pct,
        fee_rate_per_side=fee_rate_per_side,
        expected_slippage_pct=expected_slippage_pct,
    )
    micro = evaluate_microstructure(
        signal_entry=entry,
        side=side,
        qty=sizing.qty,
        ticker=ticker,
        orderbook=orderbook,
        limits=microstructure_limits,
    )
    return ExecutionRiskPlan(capital, sizing, micro)


async def final_read_only_dispatch_recheck(client, symbol: str) -> PreDispatchResult:
    """Last account-exposure check immediately before durable dispatch intent.

    This deliberately performs only authenticated reads. A read error, an
    existing position in the candidate symbol, or any active exchange order is
    fail-closed.
    """
    return await recheck_exchange_exposure(client, symbol)
