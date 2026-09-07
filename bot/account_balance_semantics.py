"""Read-only KuCoin account balance semantics shared by SHADOW and future LIVE paths.

Capital health is based on accountEquity. New-order affordability is based on
availableBalance. This module performs no exchange mutations.
"""
import math

_FIELDS = (
    "accountEquity",
    "marginBalance",
    "availableBalance",
    "unrealisedPNL",
    "positionMargin",
    "orderMargin",
    "frozenFunds",
)


def _finite_number(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} boolean")
    out = float(value)
    if not math.isfinite(out):
        raise ValueError(f"{name} nonfinite")
    return out


async def read_account_state(client):
    """Return validated equity/collateral state from KuCoin, read-only."""
    data = await client._get(
        "/api/v1/account-overview", {"currency": "USDT"}, auth=True
    )
    if not isinstance(data, dict):
        raise RuntimeError("account overview unavailable")

    equity = _finite_number(data.get("accountEquity"), "accountEquity")
    available = _finite_number(data.get("availableBalance"), "availableBalance")
    if equity < 0 or available < 0:
        raise RuntimeError("negative account balance")

    state = {key: data.get(key) for key in _FIELDS}
    state["equity"] = equity
    state["available"] = available
    state["currency"] = data.get("currency") or "USDT"
    return state


def update_risk_from_equity(risk, equity):
    """Initialize/update RiskManager from account equity only."""
    if not getattr(risk, "_ready", False):
        risk.init(equity)
    else:
        risk.update(equity)
    return risk


def collateral_allows(qty, entry, available, leverage, fee_rate):
    """Conservative free-collateral check for a prospective order."""
    if qty <= 0 or entry <= 0 or available <= 0 or leverage <= 0:
        return False, float("inf")
    required = qty * entry * (1.0 / leverage + fee_rate)
    return required <= available, required
