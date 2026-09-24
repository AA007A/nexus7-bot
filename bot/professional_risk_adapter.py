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
from typing import Any

from bot.config import cfg
from bot.kucoin import TAKER_FEE
from bot.logger import log
from bot.professional_risk import CapitalState
from bot.risk_manager_v3 import RiskManagerV3
from bot import risk_policy


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
    other legacy attributes through delegation. Only new-entry sizing and its
    operator-facing initialization telemetry are specialized here.
    ``RiskManagerV3`` remains side-effect-free and uses a full ``CapitalState``.
    """

    __slots__ = ("_legacy", "_v3", "_plans")

    def __init__(self, legacy: Any) -> None:
        # Keep ordinary self assignments so the repository's static startup
        # self-check can prove these required attributes are initialized.
        self._legacy = legacy
        self._v3 = RiskManagerV3()
        self._plans = {}

    def __getattr__(self, name: str):
        return getattr(self._legacy, name)

    def __setattr__(self, name: str, value) -> None:
        if name in self.__slots__:
            object.__setattr__(self, name, value)
        else:
            setattr(self._legacy, name, value)

    def init(self, bal: float):
        """Initialize delegated balance state without legacy risk mislabeling.

        The old ``RiskManager.init`` log called ``LEVERAGE * MAX_RISK_PCT``
        "risk per trade". That number is a notional/buying-power allocation,
        not the maximum loss at the protective stop. Executable NEXUS sizing is
        stop-aware in ``RiskManagerV3``; reporting the legacy quantity as loss
        risk can therefore overstate or understate the real planned exposure.

        State transitions remain equivalent to the legacy initializer: the
        first valid balance initializes peak/drawdown/confirmed state and sets
        ``_ready``. Only the telemetry wording/source is corrected.
        """
        if not bool(getattr(self._legacy, "_ready", False)):
            self._legacy.update(bal)
            self._legacy._ready = True
            log.info(
                "[RISK_V3_CORE] initialized balance=$%.2f leverage=%sx "
                "configured_stop_risk_pct=%.3f%%; actual projected stop loss "
                "is calculated from entry/stop geometry before dispatch",
                float(bal), cfg.LEVERAGE, float(cfg.MAX_RISK_PCT) * 100.0,
            )

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
        """Return the canonical risk-authoritative base quantity, or 0 (BLOCK).

        Delegates to ``risk_policy.size_new_entry`` which takes the minimum of
        the equity stop-risk, MAX_MARGIN_PCT collateral, operator margin CAP,
        liquidation-buffer and portfolio stop-risk caps, floored to the
        exchange lot. ``open_positions`` feeds the portfolio cap: every open
        position's projected loss at its current protective stop consumes the
        aggregate budget (unknown geometry consumes a full per-trade budget).
        """
        decision = self.size_decision(
            symbol, entry, instruments, size_mult=size_mult, open_positions=open_positions,
        )
        return float(decision.qty) if decision is not None and decision.allowed else 0.0

    def size_decision(self, symbol: str, entry: float, instruments: dict,
                      size_mult: float = 1.0, open_positions: dict | None = None):
        key = str(symbol)
        plan = self._plans.get(key)
        if plan is None:
            log.critical("[RISK_V3_CORE] %s blocked: planned geometry unavailable", key)
            return None
        if not math.isclose(float(entry), plan.entry, rel_tol=1e-9, abs_tol=1e-12):
            log.critical(
                "[RISK_V3_CORE] %s blocked: entry mismatch planned=%.12g current=%.12g",
                key, plan.entry, float(entry),
            )
            return None
        if not self._v3.confirmed:
            log.critical("[RISK_V3_CORE] %s blocked: capital state unconfirmed", key)
            return None

        try:
            multiplier = float(size_mult)
            if not math.isfinite(multiplier) or multiplier <= 0:
                raise ValueError("size_mult must be positive and finite")
            # A multiplier may shrink risk but never grow it past the plan.
            effective_risk_pct = plan.risk_pct * min(multiplier, 1.0)

            self._reconcile_latest_available()
            if not self._v3.confirmed:
                raise RuntimeError("capital state invalidated during reconciliation")

            info = (instruments or {}).get(key)
            if not isinstance(info, dict) or not info:
                raise ValueError("instrument metadata unavailable")
            policy = risk_policy.load_policy(cfg)
            cost = conservative_cost_fraction(key, policy)
            open_risks = [
                risk_policy.projected_open_risk(p, (instruments or {}).get(sym), cost)
                for sym, p in (open_positions or {}).items()
            ]
            capital = self._v3.capital
            direction = "LONG" if plan.stop < plan.entry else "SHORT"
            decision = risk_policy.size_new_entry(
                policy=policy,
                equity=capital.equity,
                available=capital.available_collateral,
                entry=float(entry),
                stop=plan.stop,
                direction=direction,
                rules=risk_policy.QuantityRules.from_instrument(info),
                cost_fraction=cost,
                risk_pct=effective_risk_pct,
                maintenance_margin_rate=_maintenance_margin_rate(info),
                open_risks=open_risks,
                max_adverse_entry_drift=max_adverse_entry_drift(),
            )
            logger = log.info if decision.allowed else log.warning
            logger("[RISK_V3_CORE] symbol=%s %s", key, decision.log_fields())
            return decision
        except Exception as exc:
            # Fail closed: any sizing failure is a BLOCK for this candidate.
            log.critical(
                "[RISK_V3_CORE] %s blocked: %s",
                key, type(exc).__name__,
            )
            return None


def conservative_cost_fraction(symbol: str, policy=None) -> float:
    """Round-trip execution cost per unit of entry notional (the larger model)."""
    from bot.kucoin_execution_model import estimated_round_trip_cost_pct

    policy = policy or risk_policy.load_policy(cfg)
    return risk_policy.round_trip_cost_fraction(
        taker_fee_per_side=float(TAKER_FEE),
        expected_slippage_pct=float(policy.expected_slippage_pct),
        modeled_round_trip_fraction=estimated_round_trip_cost_pct(symbol) / 100.0,
    )


def max_adverse_entry_drift() -> float:
    """Largest adverse signal drift the LIVE pre-dispatch guard accepts."""
    from bot.pre_dispatch_guard import limits_from_env

    bps = float(limits_from_env().max_signal_drift_bps)
    if not math.isfinite(bps) or bps < 0:
        raise ValueError("invalid NEXUS_MAX_SIGNAL_DRIFT_BPS")
    return bps / 10_000.0


def _maintenance_margin_rate(info: dict):
    for key in ("crossMaintainMarginRate", "contractMaintainMarginReference", "maintainMargin"):
        value = info.get(key)
        try:
            mmr = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(mmr) and 0 < mmr < 1:
            return mmr
    return None
