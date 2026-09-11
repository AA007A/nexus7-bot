"""Use KuCoin's margin-mode-correct maintenance margin in liquidation safety.

The public contract payload exposes ``maintainMargin``, but that value belongs
to contract / isolated-risk metadata. NEXUS-7 sends LIVE orders in CROSS margin.
KuCoin documents CROSS risk as a smooth curve and exposes the account-specific
MMR through private GET ``/api/v2/batchGetCrossOrderLimit`` using symbol,
totalMargin and leverage.

This hardening therefore treats public contract MMR as reference metadata only.
Immediately before a controlled LIVE opening it reads fresh account margin,
queries KuCoin's CROSS risk endpoint for the exact symbol at the configured
leverage, registers the returned ``mmr`` with ``bot.liquidation``, and only then
lets the existing final liquidation guard run. Any missing, malformed or failed
CROSS MMR lookup blocks the new LIVE entry. PAPER is unchanged.

Leverage, signal thresholds, sizing, stop geometry and order routing are never
changed here.
"""
from __future__ import annotations

import math


def _normalize_mmr(value):
    """Return a finite fractional maintenance-margin rate or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        mmr = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(mmr) or not (0.0 < mmr < 1.0):
        return None
    return mmr


def _positive_finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _contract_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("dataList", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def _select_cross_risk(data, kucoin_symbol):
    """Select the exact CROSS risk row; never accept another symbol's MMR."""
    for row in _contract_list(data):
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol", "")) != str(kucoin_symbol):
            continue
        mmr = _normalize_mmr(row.get("mmr"))
        if mmr is None:
            return None
        return {
            "mmr": mmr,
            "imr": _normalize_mmr(row.get("imr")),
            "totalMargin": _positive_finite(row.get("totalMargin")),
            "price": _positive_finite(row.get("price")),
            "leverage": _positive_finite(row.get("leverage")),
        }
    return None


def install(KuCoinClient, TradingEngine, liquidation, log) -> None:
    if getattr(KuCoinClient, "_official_contract_mmr_installed", False):
        return

    original_load = KuCoinClient.load_instruments
    original_open = TradingEngine._open

    async def load_instruments_with_contract_risk_metadata(self):
        instruments = await original_load(self)
        self._cross_mmr_symbols = set()

        # Public maintainMargin is useful audit/reference metadata, but it is
        # deliberately NOT registered as execution authority because LIVE uses
        # CROSS margin and KuCoin exposes a separate smooth-curve CROSS MMR.
        try:
            raw = await self._get("/api/v1/contracts/active")
            contracts = _contract_list(raw)
        except Exception as exc:
            log.warning(
                "[KUCOIN_CONTRACT_RISK] public_reference=UNAVAILABLE type=%s "
                "cross_mmr_ready=false execution_effect=BLOCK_NEW_LIVE_ENTRIES",
                type(exc).__name__,
            )
            return instruments

        by_symbol = {
            str(c.get("symbol", "")): c
            for c in contracts
            if isinstance(c, dict) and c.get("symbol")
        }
        reference_count = 0
        for standard_symbol, info in (instruments or {}).items():
            if not isinstance(info, dict):
                continue
            kucoin_symbol = str(info.get("kucoinSymbol", ""))
            contract = by_symbol.get(kucoin_symbol)
            if not contract:
                continue
            public_mmr = _normalize_mmr(contract.get("maintainMargin"))
            if public_mmr is not None:
                info["contractMaintainMarginReference"] = public_mmr
                reference_count += 1
            for source_key, dest_key in (
                ("initialMargin", "initialMarginReference"),
                ("minRiskLimit", "minRiskLimit"),
                ("maxRiskLimit", "maxRiskLimit"),
                ("riskStep", "riskStep"),
            ):
                value = _positive_finite(contract.get(source_key))
                if value is not None:
                    info[dest_key] = value

        log.warning(
            "[KUCOIN_CONTRACT_RISK] public_reference=%d/%d "
            "execution_authority=CROSS_PRIVATE_RISK_ENDPOINT "
            "endpoint=/api/v2/batchGetCrossOrderLimit leverage_unchanged=true",
            reference_count, len(instruments or {}),
        )
        return instruments

    async def open_requires_fresh_cross_mmr(self, sig, *args, **kwargs):
        # PAPER retains its existing behavior. For real controlled-pilot orders,
        # CROSS MMR must be refreshed for the exact current account margin and
        # configured leverage immediately before the existing _open path.
        if not getattr(self, "paper_trade", True) and getattr(self.pilot, "enabled", False):
            from bot.config import cfg

            info = (getattr(self, "instruments", {}) or {}).get(sig.symbol, {})
            kucoin_symbol = str(info.get("kucoinSymbol", "")) if isinstance(info, dict) else ""
            if not kucoin_symbol:
                log.warning(
                    "[KUCOIN_CROSS_RISK] symbol=%s result=BLOCK reason=missing_kucoin_symbol "
                    "execution_effect=BLOCK_NEW_LIVE_ENTRY",
                    sig.symbol,
                )
                return None

            try:
                account = await self.client._get(
                    "/api/v1/account-overview", {"currency": "USDT"}, auth=True
                )
                total_margin = None
                if isinstance(account, dict):
                    for key in ("marginBalance", "accountEquity", "equity"):
                        total_margin = _positive_finite(account.get(key))
                        if total_margin is not None:
                            break
                if total_margin is None:
                    raise ValueError("invalid_total_margin")

                response = await self.client._get(
                    "/api/v2/batchGetCrossOrderLimit",
                    {
                        "symbol": kucoin_symbol,
                        "totalMargin": f"{total_margin:.8f}",
                        "leverage": str(int(cfg.LEVERAGE)),
                    },
                    auth=True,
                )
                cross = _select_cross_risk(response, kucoin_symbol)
                if cross is None:
                    raise ValueError("cross_risk_row_unavailable")

                returned_lev = cross.get("leverage")
                if returned_lev is not None and abs(returned_lev - float(cfg.LEVERAGE)) > 1e-9:
                    raise ValueError("cross_risk_leverage_mismatch")

                liquidation.set_mmr_from_api(
                    sig.symbol, cross["mmr"], source="kucoin_cross_order_limit"
                )
                self.client._cross_mmr_symbols.add(sig.symbol)
                log.warning(
                    "[KUCOIN_CROSS_RISK] symbol=%s kucoin_symbol=%s result=PASS "
                    "mmr=%.6f total_margin=%.4f leverage=%sx source=KuCoin_private "
                    "execution_effect=NONE",
                    sig.symbol, kucoin_symbol, cross["mmr"], total_margin,
                    int(cfg.LEVERAGE),
                )
            except Exception as exc:
                log.warning(
                    "[KUCOIN_CROSS_RISK] symbol=%s result=BLOCK type=%s "
                    "reason=fresh_cross_margin_mmr_unavailable "
                    "execution_effect=BLOCK_NEW_LIVE_ENTRY",
                    sig.symbol, type(exc).__name__,
                )
                return None

            _, official = liquidation.get_mmr(sig.symbol)
            registered = sig.symbol in getattr(self.client, "_cross_mmr_symbols", set())
            if not official or not registered:
                log.warning(
                    "[KUCOIN_CROSS_RISK] symbol=%s result=BLOCK "
                    "reason=cross_mmr_registration_failed execution_effect=BLOCK_NEW_LIVE_ENTRY",
                    sig.symbol,
                )
                return None

        return await original_open(self, sig, *args, **kwargs)

    KuCoinClient.load_instruments = load_instruments_with_contract_risk_metadata
    TradingEngine._open = open_requires_fresh_cross_mmr
    KuCoinClient._official_contract_mmr_installed = True
    log.warning(
        "[KUCOIN_CONTRACT_RISK] installed: public maintainMargin is reference-only; "
        "fresh account-specific CROSS mmr from /api/v2/batchGetCrossOrderLimit is "
        "required fail-closed before controlled LIVE openings; leverage/score/sizing/" 
        "SL-TP geometry unchanged"
    )
