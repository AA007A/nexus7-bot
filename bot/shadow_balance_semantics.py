"""SHADOW-only account balance semantics.

Delegates validated read-only account parsing and collateral math to the shared
account balance semantics module, while keeping SHADOW-specific engine state and
observability here.
"""
from bot.logger import log
from bot import account_balance_semantics as account_semantics
from bot.drawdown_persistence import restore_update_real_account_peak


async def read_account_state(client):
    """Compatibility wrapper for SHADOW callers/tests."""
    return await account_semantics.read_account_state(client)


async def refresh_shadow_risk(engine):
    """Update risk from verified equity and a durable account high-water mark."""
    state = await account_semantics.read_account_state(engine.client)
    equity = state["equity"]
    available = state["available"]

    account_semantics.update_risk_from_equity(engine.risk, equity)
    await restore_update_real_account_peak(engine.risk, equity, strict=True)

    engine._shadow_available_balance = available
    engine._shadow_account_equity = equity
    engine._shadow_margin_balance = state.get("marginBalance")

    legacy = getattr(engine.risk, "_legacy", engine.risk)
    log.info(
        "[SHADOW_BALANCE_SEMANTICS] equity=%.4f available=%.4f peak_equity=%.4f "
        "drawdown=%.2f%% capital_basis=accountEquity collateral_basis=%s "
        "drawdown_basis=durable_account_equity_high_water_mark execution_effect=NONE",
        equity,
        available,
        float(getattr(legacy, "peak_balance", 0.0) or 0.0),
        float(getattr(legacy, "drawdown", 0.0) or 0.0) * 100.0,
        state.get("available_source", "unknown"),
    )
    return state


def collateral_allows(qty, entry, available, leverage, fee_rate):
    """Compatibility wrapper for SHADOW hypothetical-order collateral checks."""
    return account_semantics.collateral_allows(
        qty, entry, available, leverage, fee_rate
    )
