"""SHADOW-only account balance semantics.

Separates capital-health equity from free collateral while validation lock keeps
all exchange mutations disabled. This module performs authenticated read-only
account-overview reads only.
"""
import math

from bot.logger import log


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
    """Return validated account equity and available collateral, read-only."""
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


async def refresh_shadow_risk(engine):
    """Update RiskManager capital health from equity, not free collateral."""
    state = await read_account_state(engine.client)
    equity = state["equity"]
    available = state["available"]

    if not getattr(engine.risk, "_ready", False):
        engine.risk.init(equity)
    else:
        engine.risk.update(equity)

    engine._shadow_available_balance = available
    engine._shadow_account_equity = equity
    engine._shadow_margin_balance = state.get("marginBalance")

    log.info(
        "[SHADOW_BALANCE_SEMANTICS] equity=%.4f available=%.4f "
        "drawdown=%.2f%% capital_basis=accountEquity collateral_basis=availableBalance "
        "execution_effect=NONE",
        equity,
        available,
        engine.risk.drawdown * 100.0,
    )
    return state


def collateral_allows(qty, entry, available, leverage, fee_rate):
    """Conservative collateral sufficiency check for a hypothetical order."""
    if qty <= 0 or entry <= 0 or available <= 0 or leverage <= 0:
        return False, float("inf")
    required = qty * entry * (1.0 / leverage + fee_rate)
    return required <= available, required
