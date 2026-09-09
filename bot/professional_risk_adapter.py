"""Explicit stop-risk sizing adapter for the executable NEXUS runtime.

The canonical engine still expects the legacy ``RiskManager`` interface. This
adapter preserves that interface by delegation, but makes ``size()`` use
``RiskManagerV3`` once a validated signal geometry and capital snapshot have
been prepared by ``NexusRuntimeEngine``.

No exchange mutation, release change, or execution authorization lives here.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
from typing import Any

from bot.config import cfg
from bot.kucoin import TAKER_FEE
from bot.logger import log
from bot.professional_risk import CapitalState
from bot.risk_manager_v3 import RiskManagerV3


@dataclass(frozen=True)
class PlannedRisk:
    entry: float
    stop: float
    risk_pct: float

    def validate(self) -> "PlannedRisk":
        values = (self.entry, self.stop, self.risk_pct)
        if any(not math.isfinite(float(value)) for value in values):
            raise ValueError("planned risk contains non-finite value")
        if self.entry <= 0 or self.stop <= 0 or self.entry == self.stop:
            raise ValueError("planned entry/stop geometry is invalid")
        if not 0 < self.risk_pct <= 1:
            raise ValueError("planned risk_pct must be in (0,1]")
        return self


class ProfessionalRiskAdapter:
    """Delegate legacy risk APIs while making new-entry sizing stop-aware.

    Existing engine code continues to read/update ``balance``, ``drawdown`` and
    other legacy attributes through delegation. Only ``size`` is replaced.
    ``RiskManagerV3`` remains side-effect-free and uses a full ``CapitalState``.
    """

    __slots__ = ("_legacy", "_v3", "_plans")

    def __init__(self, legacy: Any) -> None:
        object.__setattr__(self, "_legacy", legacy)
        object.__setattr__(self, "_v3", RiskManagerV3())
        object.__setattr__(self, "_plans", {})

    def __getattr__(self, name: str):
        return getattr(self._legacy, name)

    def __setattr__(self, name: str, value) -> None:
        if name in self.__slots__:
            object.__setattr__(self, name, value)
        else:
            setattr(self._legacy, name, value)

    @property
    def professional_snapshot(self):
        return self._v3.snapshot()

    def set_plan(self, *, symbol: str, entry: float, stop: float, risk_pct: float) -> PlannedRisk:
        key = str(symbol)
        if not key:
            raise ValueError("symbol is required")
        plan = PlannedRisk(float(entry), float(stop), float(risk_pct)).validate()
        self._plans[key] = plan
        return plan

    def update_capital(self, capital: CapitalState):
        return self._v3.update_capital(capital)

    def invalidate_capital(self) -> None:
        self._v3.invalidate()

    def _reconcile_latest_available(self) -> None:
        """Use the later legacy balance read as a conservative collateral cap.

        The runtime obtains full account equity/committed-margin semantics from
        ``account-overview`` before sizing. The canonical engine then performs
        its existing fresh ``get_balance`` read immediately before ``size``.
        If that later available balance is lower, keep the lower value without
        changing equity or the risk budget.
        """
        if not self._v3.confirmed:
            return
        if not bool(getattr(self._legacy, "balance_confirmed", False)):
            return
        latest = float(getattr(self._legacy, "balance", 0.0) or 0.0)
        if not math.isfinite(latest) or latest < 0:
            self._v3.invalidate()
            return
        current = self._v3.capital
        available = min(float(current.available_collateral), latest)
        if available == current.available_collateral:
            return
        self._v3.update_capital(CapitalState(
            equity=current.equity,
            available_collateral=available,
            position_margin=current.position_margin,
            order_margin=current.order_margin,
            unrealized_pnl=current.unrealized_pnl,
        ))

    def size(self, symbol: str, entry: float, instruments: dict,
             size_mult: float = 1.0, open_positions: dict | None = None) -> float:
        """Return stop-risk-sized base quantity or fail closed with zero.

        ``open_positions`` is intentionally not used for margin arithmetic here:
        the authenticated ``CapitalState`` already separates available
        collateral, position margin, and order margin. The argument remains in
        the signature solely for compatibility with the canonical engine.
        """
        del open_positions
        key = str(symbol)
        plan = self._plans.get(key)
        if plan is None:
            log.critical("[RISK_V3_CORE] %s blocked: planned geometry unavailable", key)
            return 0.0
        if not math.isclose(float(entry), plan.entry, rel_tol=1e-9, abs_tol=1e-12):
            log.critical(
                "[RISK_V3_CORE] %s blocked: entry mismatch planned=%.12g current=%.12g",
                key, plan.entry, float(entry),
            )
            return 0.0
        if not self._v3.confirmed:
            log.critical("[RISK_V3_CORE] %s blocked: capital state unconfirmed", key)
            return 0.0

        try:
            multiplier = float(size_mult)
            if not math.isfinite(multiplier) or multiplier <= 0:
                raise ValueError("size_mult must be positive and finite")
            effective_risk_pct = plan.risk_pct * multiplier
            if not 0 < effective_risk_pct <= 1:
                raise ValueError("effective risk_pct outside (0,1]")

            self._reconcile_latest_available()
            if not self._v3.confirmed:
                raise RuntimeError("capital state invalidated during reconciliation")

            expected_slippage = float(
                os.environ.get("NEXUS_EXPECTED_SLIPPAGE_PCT", "0.001")
            )
            sizing = self._v3.size_for_stop(
                symbol=key,
                entry=float(entry),
                stop=plan.stop,
                instruments=instruments,
                risk_pct=effective_risk_pct,
                leverage=float(cfg.LEVERAGE),
                fee_rate_per_side=float(TAKER_FEE),
                expected_slippage_pct=expected_slippage,
            )
            log.info(
                "[RISK_V3_CORE] symbol=%s qty=%.12g risk_budget=%.6f "
                "projected_stop_loss=%.6f stop_distance_pct=%.6f "
                "required_margin=%.6f binding=%s decision_effect=NONE",
                key, sizing.qty, sizing.risk_budget,
                sizing.projected_stop_loss, sizing.stop_distance_pct,
                sizing.required_margin, sizing.binding_constraint,
            )
            return float(sizing.qty)
        except Exception as exc:
            log.critical(
                "[RISK_V3_CORE] %s blocked: %s",
                key, type(exc).__name__,
            )
            return 0.0
