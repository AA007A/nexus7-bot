"""Disambiguate exchange balance from PAPER and SHADOW read-only modes.

Observability-only hardening. In PAPER or validation-held SHADOW mode the
KuCoin available balance may be queried for connectivity/account visibility,
but it must never be presented as an actively executable LIVE balance.
"""
from __future__ import annotations

import math


def exchange_metadata(balance, paper_trade: bool, shadow_readonly: bool = False) -> dict:
    read_only = bool(paper_trade or shadow_readonly)
    mode = "PAPER_READ_ONLY" if paper_trade else (
        "SHADOW_READ_ONLY" if shadow_readonly else "LIVE"
    )
    return {
        "available_usdt": (
            round(float(balance), 4) if balance is not None else None
        ),
        "source": "KuCoin",
        "read_only": read_only,
        "mode": mode,
        "used_for_paper_sizing": False,
    }


def paper_metadata(balance) -> dict:
    return {
        "balance": round(float(balance or 0.0), 4),
        "source": "isolated_paper_wallet",
        "used_for_paper_sizing": True,
    }


def install(log) -> None:
    from bot.engine import TradingEngine
    from bot.kucoin import KuCoinClient, PAPER_TRADE

    if getattr(KuCoinClient, "_balance_observability_patched", False):
        return

    async def get_balance_with_clear_source(self) -> float:
        data = await self._get(
            "/api/v1/account-overview", {"currency": "USDT"}, auth=True
        )
        if not isinstance(data, dict) or "availableBalance" not in data:
            raise RuntimeError("Futures availableBalance unavailable")
        try:
            balance = float(data["availableBalance"])
            if not math.isfinite(balance):
                raise ValueError("nonfinite balance")
        except (ValueError, TypeError) as exc:
            raise RuntimeError("Futures availableBalance invalid") from exc

        self._last_exchange_balance = balance
        shadow_readonly = bool(getattr(self, "_shadow_readonly_active", False))
        if PAPER_TRADE:
            mode = "PAPER_READ_ONLY"
        elif shadow_readonly:
            mode = "SHADOW_READ_ONLY"
        else:
            mode = "LIVE"
        log.info(
            "💰 [EXCHANGE_BALANCE] USDT=$%.4f source=KuCoin "
            "mode=%s paper_sizing=FALSE",
            balance,
            mode,
        )
        return balance

    original_status = getattr(TradingEngine, "get_status", None)
    if original_status is not None and not getattr(
        TradingEngine, "_balance_observability_status_patched", False
    ):
        def status_with_balance_sources(self, *args, **kwargs):
            out = original_status(self, *args, **kwargs)
            if not isinstance(out, dict):
                return out
            result = dict(out)
            paper_trade = bool(getattr(self, "paper_trade", PAPER_TRADE))
            shadow_readonly = bool(
                getattr(self.client, "_shadow_readonly_active", False)
                or getattr(self, "_validation_safety_lock_active", False)
            )
            exchange_balance = getattr(self.client, "_last_exchange_balance", None)
            result["exchange_balance"] = exchange_metadata(
                exchange_balance,
                paper_trade,
                shadow_readonly,
            )
            if paper_trade:
                result["paper_balance_source"] = paper_metadata(
                    getattr(
                        self,
                        "_paper_balance",
                        getattr(self.risk, "balance", 0.0),
                    )
                )
            return result

        TradingEngine.get_status = status_with_balance_sources
        TradingEngine._balance_observability_status_patched = True

    KuCoinClient.get_balance = get_balance_with_clear_source
    KuCoinClient._balance_observability_patched = True
    log.info(
        "[BALANCE_OBSERVABILITY] installed: exchange/PAPER/SHADOW balance modes "
        "are explicit; sizing/risk behavior unchanged"
    )
