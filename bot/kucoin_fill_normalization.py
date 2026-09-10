"""Normalize KuCoin Futures fill quantities before engine price reconciliation.

KuCoin reports ``dealSize`` in contracts while ``dealValue`` is quote value.
The engine historically divided dealValue/dealSize directly, which produces a
quote value per contract rather than the underlying asset price whenever the
contract multiplier is not 1 (for example XRPUSDTM multiplier=10).

This runtime adapter preserves the raw contract count in
``dealSizeContracts`` and exposes ``dealSize`` to the engine in base-asset
units (contracts * multiplier). Existing filledSize/order-state semantics stay
unchanged because ``filledSize`` remains the exchange contract count.
"""
from __future__ import annotations

import math


def install(KuCoinClient, kucoin_module, log) -> None:
    if getattr(KuCoinClient, "_fill_normalization_installed", False):
        return

    original_wait_for_fill = KuCoinClient.wait_for_fill

    async def wait_for_fill_normalized(self, order_id: str, *args, **kwargs):
        out = await original_wait_for_fill(self, order_id, *args, **kwargs)
        if not isinstance(out, dict) or not out.get("filled"):
            return out

        status = out.get("status")
        if not isinstance(status, dict):
            return out

        try:
            raw_deal_size = float(status.get("dealSize", 0) or 0)
            deal_value = float(status.get("dealValue", 0) or 0)
        except (TypeError, ValueError):
            return out
        if raw_deal_size <= 0 or deal_value <= 0:
            return out

        std_symbol = kucoin_module.to_standard(str(status.get("symbol", "") or ""))
        info = getattr(self, "_instruments", {}).get(std_symbol, {})
        try:
            multiplier = float(info.get("multiplier", 0) or 0)
        except (TypeError, ValueError):
            multiplier = 0.0
        if not math.isfinite(multiplier) or multiplier <= 0:
            log.error(
                "[KUCOIN_FILL_NORMALIZATION] blocked normalization orderId=%s "
                "symbol=%s reason=missing_multiplier",
                order_id, std_symbol or status.get("symbol", "?"),
            )
            return out

        base_deal_size = raw_deal_size * multiplier
        if not math.isfinite(base_deal_size) or base_deal_size <= 0:
            return out

        normalized = dict(status)
        normalized["dealSizeContracts"] = raw_deal_size
        normalized["dealSize"] = base_deal_size
        normalized["contractMultiplier"] = multiplier
        normalized["dealValueQuote"] = deal_value
        normalized["avgDealPriceNormalized"] = deal_value / base_deal_size
        out = dict(out)
        out["status"] = normalized
        log.info(
            "[KUCOIN_FILL_NORMALIZATION] orderId=%s symbol=%s contracts=%s "
            "multiplier=%s baseQty=%s avgPrice=%.8f",
            order_id, std_symbol, raw_deal_size, multiplier, base_deal_size,
            normalized["avgDealPriceNormalized"],
        )
        return out

    KuCoinClient.wait_for_fill = wait_for_fill_normalized
    KuCoinClient._fill_normalization_installed = True
    log.warning(
        "[KUCOIN_FILL_NORMALIZATION] installed: dealSize contracts are converted "
        "to base-asset quantity before avg fill price calculation"
    )
