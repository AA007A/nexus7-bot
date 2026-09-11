"""Use KuCoin's margin-mode-correct maintenance margin in liquidation safety.

Public contract ``maintainMargin`` is reference metadata only: NEXUS-7 routes
LIVE orders in CROSS margin, where KuCoin exposes account-specific MMR through
private GET ``/api/v2/batchGetCrossOrderLimit`` using symbol, totalMargin and
leverage.

The expensive/private CROSS lookup is deliberately deferred until the core
opening path has already received an exact NEXUS approval and completed pilot
sizing. The engine then reaches ``score.calculate`` (the legacy pre-trade stage),
which acts as the last asynchronous boundary before final parameter/liquidation
checks. At that boundary this module refreshes account margin, obtains exact
symbol CROSS MMR, registers it in ``bot.liquidation``, and only then allows the
opening path to continue. Any failure produces an explicit hard-block result.

This placement avoids spending private API budget on candidates that NEXUS will
reject, while keeping the MMR fresher for the final liquidation guard. PAPER,
leverage, signal thresholds, sizing, stop geometry and order routing are never
changed here.
"""
from __future__ import annotations

import contextvars
import math


_ENGINE = contextvars.ContextVar("nexus_cross_mmr_engine", default=None)
_SIGNAL = contextvars.ContextVar("nexus_cross_mmr_signal", default=None)


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


def _hard_block_result(reason: str) -> dict:
    """Well-formed legacy-score result that cannot be advisory-bypassed."""
    return {
        "total": 0,
        "tecnico": 0,
        "orderflow": 0,
        "macro": 0,
        "news_mod": 0,
        "aprovado": False,
        "hard_block": reason,
        "detalhes": {"cross_margin_risk": reason},
    }


def install(KuCoinClient, TradingEngine, scoring, liquidation, log) -> None:
    if getattr(KuCoinClient, "_official_contract_mmr_installed", False):
        return

    original_load = KuCoinClient.load_instruments
    original_open = TradingEngine._open
    original_calculate = scoring.calculate

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

    async def _open_with_cross_context(self, sig, *args, **kwargs):
        # Context only. No private KuCoin risk call is spent before NEXUS.
        if getattr(self, "paper_trade", True) or not bool(
            getattr(getattr(self, "pilot", None), "enabled", False)
        ):
            return await original_open(self, sig, *args, **kwargs)

        token_engine = _ENGINE.set(self)
        token_signal = _SIGNAL.set(sig)
        try:
            return await original_open(self, sig, *args, **kwargs)
        finally:
            _SIGNAL.reset(token_signal)
            _ENGINE.reset(token_engine)

    async def _calculate_with_fresh_cross_mmr(
        symbol, direction, closes, highs, lows, volumes, client=None
    ):
        engine = _ENGINE.get()
        sig = _SIGNAL.get()

        # Outside the controlled LIVE opening task, scoring is untouched.
        if engine is None or sig is None:
            return await original_calculate(
                symbol, direction, closes, highs, lows, volumes, client
            )
        if str(getattr(sig, "symbol", "")) != str(symbol):
            return _hard_block_result("cross_mmr_signal_symbol_mismatch")
        if str(getattr(sig, "direction", "")).upper() != str(direction).upper():
            return _hard_block_result("cross_mmr_signal_side_mismatch")

        from bot.config import cfg

        info = (getattr(engine, "instruments", {}) or {}).get(symbol, {})
        kucoin_symbol = str(info.get("kucoinSymbol", "")) if isinstance(info, dict) else ""
        if not kucoin_symbol:
            log.warning(
                "[KUCOIN_CROSS_RISK] symbol=%s result=BLOCK reason=missing_kucoin_symbol "
                "stage=POST_NEXUS_PRETRADE execution_effect=BLOCK_NEW_LIVE_ENTRY",
                symbol,
            )
            return _hard_block_result("official_cross_mmr_unavailable")

        exchange_client = getattr(engine, "client", None)
        if exchange_client is None:
            return _hard_block_result("official_cross_mmr_unavailable")

        try:
            account = await exchange_client._get(
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

            response = await exchange_client._get(
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
                symbol, cross["mmr"], source="kucoin_cross_order_limit"
            )
            exchange_client._cross_mmr_symbols.add(symbol)
            log.warning(
                "[KUCOIN_CROSS_RISK] symbol=%s kucoin_symbol=%s result=PASS "
                "mmr=%.6f total_margin=%.4f leverage=%sx source=KuCoin_private "
                "stage=POST_NEXUS_PRETRADE execution_effect=NONE",
                symbol, kucoin_symbol, cross["mmr"], total_margin,
                int(cfg.LEVERAGE),
            )
        except Exception as exc:
            log.warning(
                "[KUCOIN_CROSS_RISK] symbol=%s result=BLOCK type=%s "
                "reason=fresh_cross_margin_mmr_unavailable stage=POST_NEXUS_PRETRADE "
                "execution_effect=BLOCK_NEW_LIVE_ENTRY",
                symbol, type(exc).__name__,
            )
            return _hard_block_result("official_cross_mmr_unavailable")

        _, official = liquidation.get_mmr(symbol)
        registered = symbol in getattr(exchange_client, "_cross_mmr_symbols", set())
        if not official or not registered:
            log.warning(
                "[KUCOIN_CROSS_RISK] symbol=%s result=BLOCK "
                "reason=cross_mmr_registration_failed stage=POST_NEXUS_PRETRADE "
                "execution_effect=BLOCK_NEW_LIVE_ENTRY",
                symbol,
            )
            return _hard_block_result("official_cross_mmr_unavailable")

        return await original_calculate(
            symbol, direction, closes, highs, lows, volumes, client
        )

    KuCoinClient.load_instruments = load_instruments_with_contract_risk_metadata
    TradingEngine._open = _open_with_cross_context
    scoring.calculate = _calculate_with_fresh_cross_mmr
    KuCoinClient._official_contract_mmr_installed = True
    log.warning(
        "[KUCOIN_CONTRACT_RISK] installed: public maintainMargin is reference-only; "
        "fresh account-specific CROSS mmr is required fail-closed at the "
        "post-NEXUS/pretrade boundary; rejected NEXUS candidates spend no CROSS-risk "
        "private request; leverage/score/sizing/SL-TP geometry unchanged"
    )
