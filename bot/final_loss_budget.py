"""Deterministic equity-based projected-loss ceiling for a new entry.

    projected_loss = qty * (|entry - stop| + entry * round_trip_cost_fraction)
    loss_limit     = equity * risk_pct

The previous authority was ``margin * 0.50``; with 50% of available collateral
as margin at 50x leverage that allowed ~25% of equity per trade, i.e. leverage
inflated the monetary loss budget. Leverage is now reported for telemetry only
and never enters the limit.

This module never resizes a quantity or moves a technical stop; it only
answers PASS/BLOCK. The sizer (``risk_policy.size_new_entry``) is expected to
produce quantities that already satisfy it. This is an estimate, not a
guaranteed maximum realized loss: gaps, funding and execution beyond the
modeled costs can exceed it.
"""
import math


class LossBudgetExceeded(ValueError):
    pass


def measure(qty, entry, stop, direction, leverage, cost_fraction, *, equity, risk_pct):
    """Return the exact arithmetic consumed by validate(), without deciding PASS/BLOCK."""
    values = (qty, entry, stop, leverage, cost_fraction, equity, risk_pct)
    if any(v is None or isinstance(v, bool) for v in values):
        raise ValueError('nonfinite loss budget')
    if any(not math.isfinite(float(v)) for v in values):
        raise ValueError('nonfinite loss budget')
    qty, entry, stop, leverage, cost_fraction, equity, risk_pct = map(float, values)
    if min(qty, entry, stop, leverage, equity) <= 0 or cost_fraction < 0:
        raise ValueError('invalid loss budget')
    if not 0 < risk_pct <= 1:
        raise ValueError('invalid loss budget')
    if not ((direction == 'LONG' and stop < entry) or (direction == 'SHORT' and stop > entry)):
        raise ValueError('invalid stop direction')

    notional = qty * entry
    margin = notional / leverage
    stop_fraction = abs(entry - stop) / entry
    projected = qty * (abs(entry - stop) + entry * cost_fraction)
    limit = equity * risk_pct
    return {
        'qty': qty,
        'entry': entry,
        'stop': stop,
        'direction': direction,
        'leverage': leverage,
        'cost_fraction': cost_fraction,
        'equity': equity,
        'risk_pct': risk_pct,
        'notional': notional,
        'margin': margin,
        'stop_fraction': stop_fraction,
        'projected_loss': projected,
        'loss_limit': limit,
        'projected_loss_pct_equity': projected / equity * 100.0,
        'allowed_loss_pct_equity': risk_pct * 100.0,
        'headroom_usdt': limit - projected,
    }


def validate(qty, entry, stop, direction, leverage, cost_fraction, *, equity, risk_pct):
    metrics = measure(
        qty, entry, stop, direction, leverage, cost_fraction,
        equity=equity, risk_pct=risk_pct,
    )
    projected = metrics['projected_loss']
    limit = metrics['loss_limit']
    if projected > limit + max(1e-12, limit * 1e-9):
        raise LossBudgetExceeded('projected loss exceeds equity risk budget')
    return projected, limit


def reason_from_exception(exc):
    text = str(exc)
    mapping = {
        'projected loss exceeds equity risk budget': 'projected_loss_exceeds_equity_risk_budget',
        'nonfinite loss budget': 'nonfinite_loss_budget',
        'invalid loss budget': 'invalid_loss_budget',
        'invalid stop direction': 'invalid_stop_direction',
    }
    return mapping.get(text, type(exc).__name__)


def emit_telemetry(
    log, *, symbol, setup_id, stage, qty, entry, stop, direction, leverage,
    cost_fraction, result, specific_reason, equity, risk_pct, risk_v3_qty=None,
):
    """Logging only. A telemetry failure never changes the PASS/BLOCK result,
    and is itself logged with the failure type (never silently swallowed)."""
    try:
        metrics = measure(
            qty, entry, stop, direction, leverage, cost_fraction,
            equity=equity, risk_pct=risk_pct,
        )
    except (TypeError, ValueError, ArithmeticError) as exc:
        log.warning(
            "[FINAL_LOSS_BUDGET] symbol=%s setup_id=%s stage=%s result=%s "
            "specific_reason=%s telemetry_error=%s",
            symbol, setup_id or 'UNKNOWN', stage, str(result).upper(),
            specific_reason, type(exc).__name__,
        )
        return
    risk_qty = 'NA' if risk_v3_qty is None else f"{float(risk_v3_qty):.12g}"
    logger = log.info if str(result).upper() == 'PASS' else log.warning
    logger(
        "[FINAL_LOSS_BUDGET] symbol=%s setup_id=%s stage=%s result=%s "
        "specific_reason=%s qty=%.12g qty_authority=RISK_POLICY_MIN_OF_CAPS "
        "risk_v3_qty=%s entry=%.12g stop=%.12g direction=%s leverage=%.12g "
        "margin=%.12g equity=%.12g risk_pct=%.8f stop_fraction=%.12g "
        "cost_fraction=%.12g projected_loss=%.12g loss_limit=%.12g "
        "projected_loss_pct_equity=%.8f allowed_loss_pct_equity=%.8f "
        "headroom_usdt=%.12g budget_basis=EQUITY_RISK_PCT",
        symbol, setup_id or 'UNKNOWN', stage, str(result).upper(),
        specific_reason, metrics['qty'], risk_qty, metrics['entry'],
        metrics['stop'], metrics['direction'], metrics['leverage'],
        metrics['margin'], metrics['equity'], metrics['risk_pct'],
        metrics['stop_fraction'], metrics['cost_fraction'],
        metrics['projected_loss'], metrics['loss_limit'],
        metrics['projected_loss_pct_equity'], metrics['allowed_loss_pct_equity'],
        metrics['headroom_usdt'],
    )
