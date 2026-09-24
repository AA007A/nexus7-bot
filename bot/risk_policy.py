"""Canonical, typed risk policy for NEXUS-7 new-exposure decisions.

This module is the single source of truth for:

* which risk configuration values are valid (``RiskPolicy.violations``);
* whether account drawdown permits NEW exposure (``drawdown_entry_decision``);
* which risk actions an operator override may ever authorize
  (``override_may_authorize``);
* the effective daily stop limit (``effective_daily_stop_limit``);
* the final quantity for a new entry (``size_new_entry``).

It is pure: no I/O, no exchange access, no logging, no global mutation. Every
runtime wrapper that previously interpreted these concepts on its own now
delegates here so two modules can no longer disagree about the same limit.

Core invariants (see RISK_INVARIANTS.md):

1. ``drawdown >= MAX_DRAWDOWN`` => new position creation is BLOCK. No
   environment variable, DRAWDOWN_MODE or operator override changes that.
2. An operator override may only authorize risk-REDUCING actions.
3. ``projected_loss_at_stop <= equity * risk_pct`` where projected loss
   includes the stop distance plus round-trip fees and expected slippage.
   Leverage never enters the monetary loss budget; it only changes collateral.
4. Margin targets are CAPS, never mandatory utilization targets.
5. If the smallest exchange-valid quantity exceeds any cap: NO TRADE.
6. The daily stop is the MORE restrictive of the percentage and absolute limit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_FLOOR
from enum import Enum
import math
import os
from typing import Iterable, Mapping


class RiskPolicyError(ValueError):
    """Raised when a risk policy cannot be evaluated safely."""


# ── Hard bounds. Values outside these are configuration errors, never clamped.
MAX_RISK_PCT_CEILING = 0.02          # 2% of equity per trade
MAX_LEVERAGE_CEILING = 125.0         # KuCoin Futures maximum
MAX_POSITIONS_CEILING = 10
MAX_DAILY_STOP_PCT_CEILING = 0.20
MAX_STRESS_RISK_RATE_CEILING = 0.90  # the previous hard-coded value is now the ceiling

DEFAULT_OPERATOR_MARGIN_CAP_PCT = 0.50
DEFAULT_MAX_STOP_STRESS_RISK_RATE = 0.50
DEFAULT_EXPECTED_SLIPPAGE_PCT = 0.001
# Used only when no maintenance-margin rate is known. It is deliberately far
# above KuCoin tier-1 rates so the liquidation cap can only get stricter.
CONSERVATIVE_FALLBACK_MMR = 0.05
# Equity left after a full stop-out must cover this multiple of maintenance.
LIQUIDATION_SAFETY_MULTIPLE = 2.0


def _finite(value, name: str) -> float:
    if isinstance(value, bool):
        raise RiskPolicyError(f"{name} must be numeric, got boolean")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise RiskPolicyError(f"{name} must be numeric") from exc
    if not math.isfinite(out):
        raise RiskPolicyError(f"{name} must be finite")
    return out


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        return float(str(raw).strip())
    except ValueError:
        # Keep the raw text visible in the violation instead of silently
        # replacing it with a default.
        return float("nan")


@dataclass(frozen=True)
class RiskPolicy:
    leverage: float
    max_risk_pct: float
    max_margin_pct: float
    max_drawdown: float
    max_positions: int
    daily_stop_loss_pct: float
    daily_stop_loss_abs: float
    min_rr_ratio: float
    operator_margin_cap_pct: float = DEFAULT_OPERATOR_MARGIN_CAP_PCT
    max_stop_stress_risk_rate: float = DEFAULT_MAX_STOP_STRESS_RISK_RATE
    expected_slippage_pct: float = DEFAULT_EXPECTED_SLIPPAGE_PCT
    post_target_risk_pct: float | None = None
    # Informational: values that were requested but have no authority.
    ignored_overrides: tuple[str, ...] = field(default_factory=tuple)

    def violations(self) -> tuple[str, ...]:
        """Return every violated invariant. Empty tuple means valid."""
        out: list[str] = []

        def bad(v) -> bool:
            return isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v))

        if bad(self.leverage) or not 1.0 <= float(self.leverage) <= MAX_LEVERAGE_CEILING:
            out.append(f"LEVERAGE={self.leverage!r} must be within [1, {MAX_LEVERAGE_CEILING:g}]")
        if bad(self.max_risk_pct) or not 0.0 < float(self.max_risk_pct) <= MAX_RISK_PCT_CEILING:
            out.append(
                f"MAX_RISK_PCT={self.max_risk_pct!r} must be within (0, {MAX_RISK_PCT_CEILING:g}]"
            )
        if self.post_target_risk_pct is not None and (
            bad(self.post_target_risk_pct)
            or not 0.0 < float(self.post_target_risk_pct) <= float(self.max_risk_pct or 0)
        ):
            out.append(
                f"POST_TARGET_RISK={self.post_target_risk_pct!r} must be within (0, MAX_RISK_PCT]"
            )
        if bad(self.max_margin_pct) or not 0.0 < float(self.max_margin_pct) <= 1.0:
            out.append(f"MAX_MARGIN_PCT={self.max_margin_pct!r} must be within (0, 1]")
        if bad(self.operator_margin_cap_pct) or not 0.0 < float(self.operator_margin_cap_pct) <= 1.0:
            out.append(
                f"OPERATOR_MARGIN_CAP_PCT={self.operator_margin_cap_pct!r} must be within (0, 1]"
            )
        if bad(self.max_drawdown) or not 0.0 < float(self.max_drawdown) < 1.0:
            out.append(f"MAX_DRAWDOWN={self.max_drawdown!r} must be within (0, 1)")
        if (
            isinstance(self.max_positions, bool)
            or not isinstance(self.max_positions, int)
            or not 1 <= self.max_positions <= MAX_POSITIONS_CEILING
        ):
            out.append(f"MAX_POSITIONS={self.max_positions!r} must be an int within [1, {MAX_POSITIONS_CEILING}]")
        if bad(self.daily_stop_loss_pct) or not 0.0 < float(self.daily_stop_loss_pct) <= MAX_DAILY_STOP_PCT_CEILING:
            out.append(
                f"DAILY_STOP_LOSS_PCT={self.daily_stop_loss_pct!r} must be within (0, {MAX_DAILY_STOP_PCT_CEILING:g}]"
            )
        if bad(self.daily_stop_loss_abs) or float(self.daily_stop_loss_abs) < 0.0:
            out.append(f"DAILY_STOP_LOSS={self.daily_stop_loss_abs!r} must be finite and >= 0 (0 disables)")
        if bad(self.min_rr_ratio) or float(self.min_rr_ratio) <= 0.0:
            out.append(f"MIN_RR_RATIO={self.min_rr_ratio!r} must be > 0")
        if bad(self.max_stop_stress_risk_rate) or not 0.0 < float(self.max_stop_stress_risk_rate) <= MAX_STRESS_RISK_RATE_CEILING:
            out.append(
                f"MAX_STOP_STRESS_RISK_RATE={self.max_stop_stress_risk_rate!r} must be within (0, {MAX_STRESS_RISK_RATE_CEILING:g}]"
            )
        if bad(self.expected_slippage_pct) or not 0.0 <= float(self.expected_slippage_pct) < 0.05:
            out.append(
                f"NEXUS_EXPECTED_SLIPPAGE_PCT={self.expected_slippage_pct!r} must be within [0, 0.05)"
            )
        # Cross-field contradictions: a single trade must not be able to
        # breach the daily circuit breaker or the drawdown limit on its own.
        if not out:
            if self.max_risk_pct >= self.daily_stop_loss_pct:
                out.append(
                    f"MAX_RISK_PCT={self.max_risk_pct:g} >= DAILY_STOP_LOSS_PCT={self.daily_stop_loss_pct:g}: "
                    "one stop-out would exceed the daily circuit breaker"
                )
            if self.max_risk_pct >= self.max_drawdown:
                out.append(
                    f"MAX_RISK_PCT={self.max_risk_pct:g} >= MAX_DRAWDOWN={self.max_drawdown:g}"
                )
        return tuple(out)

    @property
    def valid(self) -> bool:
        return not self.violations()

    def summary(self) -> str:
        return (
            f"leverage={self.leverage:g} max_risk_pct={self.max_risk_pct:g} "
            f"max_margin_pct={self.max_margin_pct:g} operator_margin_cap_pct={self.operator_margin_cap_pct:g} "
            f"max_drawdown={self.max_drawdown:g} max_positions={self.max_positions} "
            f"daily_stop_loss_pct={self.daily_stop_loss_pct:g} daily_stop_loss_abs={self.daily_stop_loss_abs:g} "
            f"min_rr_ratio={self.min_rr_ratio:g} max_stop_stress_risk_rate={self.max_stop_stress_risk_rate:g} "
            f"expected_slippage_pct={self.expected_slippage_pct:g}"
        )


# Environment variables that USED to widen risk and now have no authority for
# new exposure. Their presence is reported, never honored.
NON_AUTHORITATIVE_OVERRIDES = (
    "LIVE_RISK_OVERRIDE_APPROVED",
    "DAILY_STOP_OPERATOR_OVERRIDE",
)


def load_policy(cfg_obj=None, env: Mapping[str, str] | None = None) -> RiskPolicy:
    """Build the policy from the existing ``cfg`` values plus policy-only env.

    ``cfg`` remains the parser for legacy variables; this function is the one
    place that decides whether their combination is acceptable.
    """
    if cfg_obj is None:
        from bot.config import cfg as cfg_obj  # local import keeps module pure for tests
    env = os.environ if env is None else env
    ignored = tuple(
        name for name in NON_AUTHORITATIVE_OVERRIDES
        if str(env.get(name, "") or "").strip().lower() == "true"
    )
    max_positions = getattr(cfg_obj, "MAX_POSITIONS", 0)
    return RiskPolicy(
        leverage=float(getattr(cfg_obj, "LEVERAGE", float("nan"))),
        max_risk_pct=float(getattr(cfg_obj, "MAX_RISK_PCT", float("nan"))),
        max_margin_pct=float(getattr(cfg_obj, "MAX_MARGIN_PCT", float("nan"))),
        max_drawdown=float(getattr(cfg_obj, "MAX_DRAWDOWN", float("nan"))),
        max_positions=max_positions if isinstance(max_positions, int) else -1,
        daily_stop_loss_pct=float(getattr(cfg_obj, "DAILY_STOP_LOSS_PCT", float("nan"))),
        daily_stop_loss_abs=float(getattr(cfg_obj, "DAILY_STOP_LOSS", 0.0) or 0.0),
        min_rr_ratio=float(getattr(cfg_obj, "MIN_RR_RATIO", float("nan"))),
        operator_margin_cap_pct=_env_float(env, "OPERATOR_MARGIN_CAP_PCT", DEFAULT_OPERATOR_MARGIN_CAP_PCT),
        max_stop_stress_risk_rate=_env_float(env, "MAX_STOP_STRESS_RISK_RATE", DEFAULT_MAX_STOP_STRESS_RISK_RATE),
        expected_slippage_pct=_env_float(env, "NEXUS_EXPECTED_SLIPPAGE_PCT", DEFAULT_EXPECTED_SLIPPAGE_PCT),
        post_target_risk_pct=(
            float(getattr(cfg_obj, "POST_TARGET_RISK"))
            if getattr(cfg_obj, "POST_TARGET_RISK", None) is not None else None
        ),
        ignored_overrides=ignored,
    )


# ── Drawdown ─────────────────────────────────────────────────────────────────
class EntryDecision(str, Enum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"


@dataclass(frozen=True)
class DrawdownDecision:
    decision: EntryDecision
    drawdown: float
    limit: float
    override_requested: bool
    reason: str

    @property
    def can_open(self) -> bool:
        return self.decision is EntryDecision.ALLOW


def drawdown_entry_decision(
    drawdown, max_drawdown, *, override_requested: bool = False
) -> DrawdownDecision:
    """Hard gate: drawdown at/over the limit blocks every new position.

    ``override_requested`` is accepted solely so the caller's telemetry is
    truthful; it cannot change the decision.
    """
    try:
        dd = _finite(drawdown, "drawdown")
        limit = _finite(max_drawdown, "max_drawdown")
    except RiskPolicyError as exc:
        return DrawdownDecision(EntryDecision.BLOCK, float("nan"), float("nan"),
                                bool(override_requested), f"invalid_input:{exc}")
    if dd < 0 or not 0 < limit < 1:
        return DrawdownDecision(EntryDecision.BLOCK, dd, limit, bool(override_requested),
                                "invalid_drawdown_state")
    if dd >= limit:
        return DrawdownDecision(EntryDecision.BLOCK, dd, limit, bool(override_requested),
                                "max_drawdown_reached")
    return DrawdownDecision(EntryDecision.ALLOW, dd, limit, bool(override_requested), "within_limit")


# ── Actions an override may authorize ────────────────────────────────────────
class RiskAction(str, Enum):
    OPEN_POSITION = "OPEN_POSITION"
    INCREASE_POSITION = "INCREASE_POSITION"
    CLOSE_POSITION = "CLOSE_POSITION"
    REDUCE_POSITION = "REDUCE_POSITION"
    CANCEL_EXPOSURE_INCREASING_ORDER = "CANCEL_EXPOSURE_INCREASING_ORDER"
    INSTALL_OR_REPAIR_PROTECTION = "INSTALL_OR_REPAIR_PROTECTION"
    RECONCILE = "RECONCILE"


RISK_REDUCING_ACTIONS = frozenset({
    RiskAction.CLOSE_POSITION,
    RiskAction.REDUCE_POSITION,
    RiskAction.CANCEL_EXPOSURE_INCREASING_ORDER,
    RiskAction.INSTALL_OR_REPAIR_PROTECTION,
    RiskAction.RECONCILE,
})


def override_may_authorize(action: RiskAction) -> bool:
    """An emergency/operator override can never add directional exposure."""
    return RiskAction(action) in RISK_REDUCING_ACTIONS


# ── Daily stop ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DailyStopLimit:
    limit: float
    pct_limit: float
    absolute_limit: float | None
    source: str


def effective_daily_stop_limit(balance, policy_or_pct, absolute=None) -> DailyStopLimit:
    """Return the MORE restrictive of the percentage and absolute daily loss.

    ``absolute`` <= 0 or None means "not configured". A non-finite absolute is
    treated as invalid and ignored (the percentage still governs), so a broken
    absolute value can only fail towards the stricter percentage limit.
    """
    if isinstance(policy_or_pct, RiskPolicy):
        pct = policy_or_pct.daily_stop_loss_pct
        absolute = policy_or_pct.daily_stop_loss_abs if absolute is None else absolute
    else:
        pct = policy_or_pct
    bal = _finite(balance, "balance")
    pct = _finite(pct, "daily_stop_loss_pct")
    if bal <= 0:
        raise RiskPolicyError("daily stop requires a positive confirmed balance")
    if not 0 < pct <= MAX_DAILY_STOP_PCT_CEILING:
        raise RiskPolicyError("daily stop percentage outside valid range")
    pct_limit = round(bal * pct, 2)
    if pct_limit <= 0:
        # Sub-cent accounts: keep a strictly positive limit rather than 0,
        # which downstream readers treat as "not configured".
        pct_limit = bal * pct
    abs_limit = None
    if absolute is not None and not isinstance(absolute, bool):
        try:
            a = float(absolute)
        except (TypeError, ValueError):
            a = float("nan")
        if math.isfinite(a) and a > 0:
            abs_limit = a
    if abs_limit is not None and abs_limit < pct_limit:
        return DailyStopLimit(abs_limit, pct_limit, abs_limit, "ABSOLUTE_STRICTER")
    return DailyStopLimit(pct_limit, pct_limit, abs_limit, "PERCENT_STRICTER" if abs_limit else "PERCENT_ONLY")


# ── Position sizing ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class QuantityRules:
    """Exchange quantity rules in native contracts plus base/contract multiplier."""
    multiplier: Decimal
    lot: Decimal
    min_contracts: Decimal
    min_notional: Decimal

    @classmethod
    def from_instrument(cls, info: Mapping) -> "QuantityRules":
        from bot.quantity import quantity_rules
        multiplier, lot, minimum, notional = quantity_rules(info)
        return cls(multiplier, lot, minimum, notional)

    @property
    def base_step(self) -> Decimal:
        return self.multiplier * self.lot

    def floor_base(self, qty: float) -> Decimal:
        if not math.isfinite(qty) or qty <= 0:
            return Decimal(0)
        steps = (Decimal(str(qty)) / self.base_step).to_integral_value(rounding=ROUND_FLOOR)
        return steps * self.base_step

    def minimum_base(self, price: float) -> Decimal:
        from decimal import ROUND_CEILING
        price_d = Decimal(str(price))
        contracts = max(self.min_contracts, self.min_notional / (price_d * self.multiplier))
        contracts = (contracts / self.lot).to_integral_value(rounding=ROUND_CEILING) * self.lot
        return contracts * self.multiplier


@dataclass(frozen=True)
class OpenRisk:
    """Projected loss at stop of one already open position (>= 0)."""
    symbol: str
    projected_loss: float


@dataclass(frozen=True)
class SizingDecision:
    qty: float
    decision: EntryDecision
    binding_constraint: str
    reason: str
    equity: float
    available: float
    risk_pct: float
    risk_budget: float
    projected_loss_at_stop: float
    loss_per_unit: float
    cost_fraction: float
    stop_distance_pct: float
    leverage: float
    notional: float
    required_margin: float
    caps: Mapping[str, float]

    @property
    def allowed(self) -> bool:
        return self.decision is EntryDecision.ALLOW and self.qty > 0

    def log_fields(self) -> str:
        caps = " ".join(f"cap_{k}={v:.12g}" for k, v in sorted(self.caps.items()))
        return (
            f"decision={self.decision.value} qty={self.qty:.12g} binding={self.binding_constraint} "
            f"reason={self.reason} equity={self.equity:.6f} available={self.available:.6f} "
            f"risk_pct={self.risk_pct:.6f} risk_budget={self.risk_budget:.6f} "
            f"projected_loss_at_stop={self.projected_loss_at_stop:.6f} "
            f"cost_fraction={self.cost_fraction:.6f} stop_distance_pct={self.stop_distance_pct:.6f} "
            f"leverage={self.leverage:g} notional={self.notional:.6f} "
            f"required_margin={self.required_margin:.6f} {caps}"
        )


def round_trip_cost_fraction(*, taker_fee_per_side: float, expected_slippage_pct: float,
                             modeled_round_trip_fraction: float | None = None) -> float:
    """Conservative cost per unit of entry notional: the larger of the models."""
    fee = _finite(taker_fee_per_side, "taker_fee_per_side")
    slip = _finite(expected_slippage_pct, "expected_slippage_pct")
    if fee < 0 or slip < 0:
        raise RiskPolicyError("fees/slippage cannot be negative")
    base = 2.0 * fee + slip
    if modeled_round_trip_fraction is not None:
        modeled = _finite(modeled_round_trip_fraction, "modeled_round_trip_fraction")
        if modeled < 0:
            raise RiskPolicyError("modeled round trip cost cannot be negative")
        base = max(base, modeled)
    return base


def _blocked(reason: str, binding: str, **kw) -> SizingDecision:
    defaults = dict(
        equity=float("nan"), available=float("nan"), risk_pct=float("nan"),
        risk_budget=0.0, projected_loss_at_stop=0.0, loss_per_unit=float("nan"),
        cost_fraction=float("nan"), stop_distance_pct=float("nan"),
        leverage=float("nan"), notional=0.0, required_margin=0.0, caps={},
    )
    defaults.update(kw)
    return SizingDecision(qty=0.0, decision=EntryDecision.BLOCK,
                          binding_constraint=binding, reason=reason, **defaults)


def size_new_entry(
    *,
    policy: RiskPolicy,
    equity: float,
    available: float,
    entry: float,
    stop: float,
    direction: str,
    rules: QuantityRules,
    cost_fraction: float,
    risk_pct: float | None = None,
    leverage: float | None = None,
    maintenance_margin_rate: float | None = None,
    open_risks: Iterable[OpenRisk] = (),
    max_adverse_entry_drift: float = 0.0,
) -> SizingDecision:
    """Compute the final new-entry quantity as the minimum of all safe caps.

    Caps (base-asset quantity):
      risk        equity * risk_pct / (stop distance + round-trip costs)
      margin      available * MAX_MARGIN_PCT * leverage / entry
      operator    available * OPERATOR_MARGIN_CAP_PCT * leverage / entry (a CAP only)
      liquidation keep equity after a full stop-out >= LIQUIDATION_SAFETY_MULTIPLE
                  * maintenance margin of the position (CROSS approximation)
      portfolio   remaining aggregate stop-risk budget
                  (MAX_POSITIONS * equity * MAX_RISK_PCT - open projected losses)
    The minimum is floored to the exchange lot. If that is below the exchange
    minimum, the entry is BLOCKED: quantity is never escalated to the minimum.

    ``max_adverse_entry_drift`` (fraction) sizes for the worst fill the
    pre-dispatch guard will still accept, so the fresh-price recheck of the
    same loss budget holds for every accepted fill price.
    """
    violations = policy.violations()
    if violations:
        return _blocked("risk_policy_invalid:" + "|".join(violations), "RISK_POLICY")

    try:
        eq = _finite(equity, "equity")
        avail = _finite(available, "available")
        px = _finite(entry, "entry")
        sl = _finite(stop, "stop")
        cost = _finite(cost_fraction, "cost_fraction")
        lev = _finite(policy.leverage if leverage is None else leverage, "leverage")
        rpct = _finite(policy.max_risk_pct if risk_pct is None else risk_pct, "risk_pct")
        drift = _finite(max_adverse_entry_drift, "max_adverse_entry_drift")
    except RiskPolicyError as exc:
        return _blocked(f"invalid_input:{exc}", "INPUT")

    side = str(direction or "").upper()
    if eq <= 0 or avail <= 0:
        return _blocked("nonpositive_capital", "CAPITAL", equity=eq, available=avail)
    if px <= 0 or sl <= 0 or px == sl:
        return _blocked("invalid_entry_stop", "GEOMETRY", equity=eq, available=avail)
    if not ((side == "LONG" and sl < px) or (side == "SHORT" and sl > px)):
        return _blocked("stop_not_protective_for_direction", "GEOMETRY", equity=eq, available=avail)
    if cost < 0 or not 0 <= drift < 0.05:
        return _blocked("invalid_cost_or_drift", "INPUT", equity=eq, available=avail)
    if not 1.0 <= lev <= MAX_LEVERAGE_CEILING:
        return _blocked("leverage_out_of_bounds", "INPUT", equity=eq, available=avail)
    # The per-trade risk can be reduced (post-target, size multipliers) but
    # can never exceed the configured MAX_RISK_PCT.
    if not 0 < rpct <= policy.max_risk_pct:
        return _blocked("risk_pct_exceeds_policy", "RISK_POLICY", equity=eq, available=avail)

    stop_distance = abs(px - sl)
    # Worst accepted fill: entry moved adversely by ``drift``; costs scale
    # with the fill notional.
    loss_per_unit = stop_distance + px * drift + px * (1.0 + drift) * cost
    risk_budget = eq * rpct

    caps: dict[str, float] = {}
    caps["risk"] = risk_budget / loss_per_unit
    caps["margin"] = avail * policy.max_margin_pct * lev / px
    caps["operator"] = avail * policy.operator_margin_cap_pct * lev / px

    mmr = maintenance_margin_rate
    if mmr is None or isinstance(mmr, bool) or not math.isfinite(float(mmr)) or not 0 < float(mmr) < 1:
        mmr = CONSERVATIVE_FALLBACK_MMR
    mmr = float(mmr)
    # equity - qty*loss_per_unit >= K * mmr * qty * px
    caps["liquidation"] = eq / (loss_per_unit + LIQUIDATION_SAFETY_MULTIPLE * mmr * px)

    existing = 0.0
    n_open = 0
    for item in open_risks:
        n_open += 1
        try:
            loss = _finite(item.projected_loss, f"open_risk[{item.symbol}]")
        except RiskPolicyError:
            # Unknown existing exposure consumes a full per-trade budget.
            loss = eq * policy.max_risk_pct
        existing += max(loss, 0.0)
    if n_open >= policy.max_positions:
        return _blocked("max_positions_reached", "PORTFOLIO", equity=eq, available=avail, caps=caps)
    portfolio_budget = policy.max_positions * eq * policy.max_risk_pct - existing
    caps["portfolio"] = max(portfolio_budget, 0.0) / loss_per_unit

    binding = min(caps, key=lambda k: caps[k])
    raw = caps[binding]
    qty_d = rules.floor_base(raw)
    min_d = rules.minimum_base(px)
    common = dict(
        equity=eq, available=avail, risk_pct=rpct, risk_budget=risk_budget,
        loss_per_unit=loss_per_unit, cost_fraction=cost,
        stop_distance_pct=stop_distance / px, leverage=lev, caps=caps,
    )
    if qty_d <= 0 or qty_d < min_d:
        return _blocked("exchange_minimum_exceeds_safe_quantity", "MINIMUM_ORDER", **common)

    qty = float(qty_d)
    projected = qty * loss_per_unit
    notional = qty * px
    margin = notional / lev
    # Post-rounding invariants. Flooring can only shrink, so these are proofs,
    # not adjustments; any failure is a bug and blocks.
    if projected > risk_budget * (1 + 1e-9):
        return _blocked("post_round_risk_budget_exceeded", "INVARIANT", **common)
    if margin > avail * min(policy.max_margin_pct, policy.operator_margin_cap_pct) * (1 + 1e-9):
        return _blocked("post_round_margin_cap_exceeded", "INVARIANT", **common)

    return SizingDecision(
        qty=qty, decision=EntryDecision.ALLOW,
        binding_constraint=binding.upper(), reason="all_caps_satisfied",
        projected_loss_at_stop=projected, notional=notional, required_margin=margin,
        **common,
    )


def projected_open_risk(position, info: Mapping | None, cost_fraction: float) -> OpenRisk:
    """Projected loss if an open position stops out now (never negative).

    Missing geometry yields NaN, which ``size_new_entry`` treats as a full
    per-trade budget (conservative).
    """
    symbol = str(getattr(position, "symbol", "?"))
    try:
        qty = float(getattr(position, "qty", float("nan")))
        entry = float(getattr(position, "entry", float("nan")))
        stop = getattr(position, "trailing_sl", None) or getattr(position, "sl", None)
        stop = float(stop) if stop is not None else float("nan")
        direction = str(getattr(position, "direction", "")).upper()
        if not all(math.isfinite(v) and v > 0 for v in (qty, entry, stop)):
            return OpenRisk(symbol, float("nan"))
        if direction == "LONG":
            price_loss = max(entry - stop, 0.0)
        elif direction == "SHORT":
            price_loss = max(stop - entry, 0.0)
        else:
            return OpenRisk(symbol, float("nan"))
        return OpenRisk(symbol, qty * (price_loss + entry * float(cost_fraction)))
    except (TypeError, ValueError):
        return OpenRisk(symbol, float("nan"))
