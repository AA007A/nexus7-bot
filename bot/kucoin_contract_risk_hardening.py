"""Use KuCoin's public contract maintenance margin in liquidation safety.

KuCoin's /api/v1/contracts/active response exposes ``maintainMargin`` for each
contract. The liquidation guard historically fell back to a global 0.4% MMR
when no symbol-specific value had been registered. That fallback is acceptable
for diagnostics, but it is not sufficient authority for a 50x LIVE opening:
maintenance margin is contract-specific and materially changes liquidation
distance.

This runtime hardening performs one extra public contract refresh after the
normal instrument loader, registers each matched symbol's official
``maintainMargin`` with ``bot.liquidation``, and makes official MMR availability
a fail-closed prerequisite for controlled LIVE openings. It never changes
leverage, signal thresholds, sizing, stop geometry, or order routing.
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


def _contract_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("dataList", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def install(KuCoinClient, TradingEngine, liquidation, log) -> None:
    if getattr(KuCoinClient, "_official_contract_mmr_installed", False):
        return

    original_load = KuCoinClient.load_instruments
    original_open = TradingEngine._open

    async def load_instruments_with_official_mmr(self):
        instruments = await original_load(self)
        self._official_mmr_symbols = set()

        try:
            raw = await self._get("/api/v1/contracts/active")
            contracts = _contract_list(raw)
        except Exception as exc:
            log.warning(
                "[KUCOIN_CONTRACT_RISK] result=UNAVAILABLE type=%s "
                "official_mmr_ready=false execution_effect=BLOCK_NEW_LIVE_ENTRIES",
                type(exc).__name__,
            )
            return instruments

        by_symbol = {
            str(c.get("symbol", "")): c
            for c in contracts
            if isinstance(c, dict) and c.get("symbol")
        }
        missing = []
        for standard_symbol, info in (instruments or {}).items():
            if not isinstance(info, dict):
                missing.append(str(standard_symbol))
                continue
            kucoin_symbol = str(info.get("kucoinSymbol", ""))
            contract = by_symbol.get(kucoin_symbol)
            mmr = _normalize_mmr(contract.get("maintainMargin") if contract else None)
            if mmr is None:
                missing.append(str(standard_symbol))
                continue

            liquidation.set_mmr_from_api(
                str(standard_symbol), mmr, source="kucoin_contracts_active"
            )
            info["maintainMargin"] = mmr
            # Keep adjacent public risk metadata for diagnostics/audits.
            for source_key, dest_key in (
                ("initialMargin", "initialMargin"),
                ("minRiskLimit", "minRiskLimit"),
                ("maxRiskLimit", "maxRiskLimit"),
                ("riskStep", "riskStep"),
            ):
                value = contract.get(source_key)
                if value is not None and not isinstance(value, bool):
                    try:
                        normalized = float(value)
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(normalized):
                        info[dest_key] = normalized
            self._official_mmr_symbols.add(str(standard_symbol))

        log.warning(
            "[KUCOIN_CONTRACT_RISK] result=%s official_mmr=%d/%d missing=%s "
            "source=/api/v1/contracts/active leverage_unchanged=true execution_effect=%s",
            "PASS" if not missing else "PARTIAL",
            len(self._official_mmr_symbols), len(instruments or {}),
            ",".join(missing) if missing else "NONE",
            "NONE" if not missing else "BLOCK_MISSING_SYMBOLS_ONLY",
        )
        return instruments

    async def open_requires_official_mmr(self, sig, *args, **kwargs):
        # PAPER retains its existing behavior. This extra authority is needed
        # only when a real controlled-pilot order could reach KuCoin.
        if not getattr(self, "paper_trade", True) and getattr(self.pilot, "enabled", False):
            _, official = liquidation.get_mmr(sig.symbol)
            registered = sig.symbol in getattr(self.client, "_official_mmr_symbols", set())
            if not official or not registered:
                log.warning(
                    "[KUCOIN_CONTRACT_RISK] symbol=%s result=BLOCK "
                    "reason=official_maintenance_margin_unavailable "
                    "execution_effect=BLOCK_NEW_LIVE_ENTRY",
                    sig.symbol,
                )
                return None
        return await original_open(self, sig, *args, **kwargs)

    KuCoinClient.load_instruments = load_instruments_with_official_mmr
    TradingEngine._open = open_requires_official_mmr
    KuCoinClient._official_contract_mmr_installed = True
    log.warning(
        "[KUCOIN_CONTRACT_RISK] installed: KuCoin maintainMargin is registered "
        "per symbol and required fail-closed for controlled LIVE openings; "
        "leverage/score/sizing/SL-TP geometry unchanged"
    )
