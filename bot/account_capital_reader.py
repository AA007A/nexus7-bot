"""Authenticated, read-only capital snapshot reader for NEXUS-7.

This module centralizes the KuCoin account-overview read needed by the new risk
path. It never performs exchange mutation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from bot.professional_risk import CapitalState, capital_state_from_account_overview


@dataclass(frozen=True)
class AccountCapitalSnapshot:
    capital: CapitalState
    raw: Mapping[str, Any]


async def read_account_capital(client, currency: str = "USDT") -> AccountCapitalSnapshot:
    """Read and normalize KuCoin Futures account capital fail-closed.

    The existing `get_balance()` API only returns availableBalance. Professional
    risk management requires the full account overview so equity cannot be
    confused with free collateral.
    """
    data = await client._get(
        "/api/v1/account-overview",
        {"currency": currency},
        auth=True,
    )
    if not isinstance(data, Mapping):
        raise RuntimeError("account overview unavailable")
    capital = capital_state_from_account_overview(data)
    return AccountCapitalSnapshot(capital=capital, raw=data)
