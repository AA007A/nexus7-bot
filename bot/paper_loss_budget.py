"""PAPER-only projected stop-loss budget; not a guarantee against market gaps.

No environment changes, wallet resets or LIVE sizing changes. Slippage reserve
matches the existing NEXUS cost model (0.05% per side). Funding is not modeled.
"""
from decimal import Decimal, ROUND_FLOOR

from bot.quantity import number, quantity_rules, validate_base_quantity

PAPER_LOSS_FRACTION = Decimal("0.70")
SLIPPAGE_PER_SIDE = Decimal("0.0005")


def cap_quantity(qty, balance, entry, stop, direction, info, taker_fee):
    """Floor quantity to valid base lots; invalid/missing inputs fail closed."""
    qty, balance, entry, stop = (
        number(value, positive=True) for value in (qty, balance, entry, stop)
    )
    fee = number(taker_fee, positive=True)
    if not ((direction == "LONG" and stop < entry)
            or (direction == "SHORT" and stop > entry)):
        raise ValueError("invalid protective stop")
    # Reserve adverse slippage on both legs plus fees on enlarged notionals.
    unit_loss = (abs(entry - stop)
                 + (entry + stop) * SLIPPAGE_PER_SIDE
                 + (entry + stop) * (1 + SLIPPAGE_PER_SIDE) * fee)
    multiplier, lot, _, _ = quantity_rules(info)
    step = multiplier * lot
    budget = balance * PAPER_LOSS_FRACTION
    capped = (min(qty, budget / unit_loss) / step).to_integral_value(
        rounding=ROUND_FLOOR
    ) * step
    # Never increase to satisfy an exchange minimum or round upward via float.
    result = float(capped)
    validate_base_quantity(result, info, entry)
    if number(result) * unit_loss > budget or number(result) > qty:
        raise ValueError("rounded quantity exceeds PAPER loss budget")
    return result
