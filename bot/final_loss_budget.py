"""Deterministic projected loss ceiling for the operator margin policy.

Keep 50% available margin and configured leverage. Reject incompatible stops;
never resize or move a technical stop. This is an estimate, not a guaranteed
maximum realized loss: gaps, funding and execution beyond estimates can exceed it.
"""
import math


def validate(qty, entry, stop, direction, leverage, cost_fraction):
    values = (qty, entry, stop, leverage, cost_fraction)
    if any(isinstance(v, bool) or not math.isfinite(float(v)) for v in values):
        raise ValueError('nonfinite loss budget')
    qty, entry, stop, leverage, cost_fraction = map(float, values)
    if min(qty, entry, stop, leverage) <= 0 or cost_fraction < 0:
        raise ValueError('invalid loss budget')
    if not ((direction == 'LONG' and stop < entry) or (direction == 'SHORT' and stop > entry)):
        raise ValueError('invalid stop direction')
    margin = qty * entry / leverage
    projected = qty * (abs(entry - stop) + entry * cost_fraction)
    limit = margin * 0.50
    if projected > limit + max(1e-12, limit * 1e-12):
        raise ValueError('projected loss exceeds 50pct entry margin')
    return projected, limit
