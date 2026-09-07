"""SHADOW-only account balance semantics.

Delegates validated read-only account parsing and collateral math to the shared
account balance semantics module, while keeping SHADOW-specific engine state and
observability here.
"""
from bot.logger import log
from bot import account_balance_semantics as account_semantics


async def read_account_state(client):
    """Compatibility wrapper for SHADOW callers/tests."""
    return await account_semantics.read_account_state(client)


async def refresh_shadow_risk(engine):
    """Update RiskManager capital health from equity, not free collateral."""
    state = await account_semantics.read_account_state(engine.client)
    equity = state["equity"]
    available = state["available"]

    account_semantics.update_risk_from_equity(engine.risk, equity)

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
    """Compatibility wrapper for SHADOW hypothetical-order collateral checks."""
    return account_semantics.collateral_allows(
        qty, entry, available, leverage, fee_rate
    )
