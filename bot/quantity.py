"""KuCoin quantity boundaries.

minQty/lotSize/qtyStep: integer contracts. multiplier: base asset/contract.
minNotional: quote currency (USDT), zero if the exchange has no such rule.
All engine/Position/RiskManager quantities are base asset. Only _round_qty
converts an outgoing base quantity to native contracts.
"""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR


def number(value, *, positive=False):
    if isinstance(value, bool):
        raise ValueError('boolean is not a quantity')
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError('invalid quantity metadata')
    return result


def quantity_rules(info):
    multiplier = number(info['multiplier'], positive=True)
    lot = number(info.get('lotSize', info.get('qtyStep', info['minQty'])), positive=True)
    minimum = number(info['minQty'], positive=True)
    if lot != lot.to_integral_value() or minimum != minimum.to_integral_value():
        raise ValueError('KuCoin lotSize/minQty must be integer contracts')
    notional = number(info.get('minNotional', 0))
    return multiplier, lot, minimum, notional


def minimum_base_quantity(info, price):
    """Smallest valid order as base asset, rounded UP to a whole native lot."""
    multiplier, lot, minimum, notional = quantity_rules(info)
    price = number(price, positive=True)
    contracts = max(minimum, notional / (price * multiplier))
    contracts = (contracts / lot).to_integral_value(rounding=ROUND_CEILING) * lot
    return float(contracts * multiplier)


def validate_base_quantity(qty, info, price):
    """Validate in base units; do not convert an outgoing qty to contracts."""
    multiplier, lot, minimum, notional = quantity_rules(info)
    qty, price = number(qty, positive=True), number(price, positive=True)
    if qty < minimum * multiplier or qty % (lot * multiplier) != 0:
        raise ValueError('base qty violates minQty or lotSize')
    if qty * price < notional:
        raise ValueError('quote notional below exchange minimum')


def base_to_contracts(qty, info):
    """Single outgoing conversion. Floor to a native lot; never grow exposure."""
    multiplier, lot, minimum, _ = quantity_rules(info)
    qty = number(qty, positive=True)
    contracts = (qty / (multiplier * lot)).to_integral_value(rounding=ROUND_FLOOR) * lot
    if contracts < minimum:
        raise ValueError("quantity below exchange minimum")
    return int(contracts)
