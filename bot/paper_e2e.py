"""PAPER-only E2E helper.

Keeps the legacy pre-trade scorer fully active for diagnostics, but in PAPER
mode a rejection is shadow-only so the simulator can exercise the complete
candidate -> NEXUS -> sizing -> order -> management -> close lifecycle.

Live trading is untouched: the same pre-trade rejection remains blocking.
This module never changes PAPER_TRADE, live confirmations, leverage, sizing,
NEXUS thresholds, liquidation checks, integrity gates, or exchange order code.
"""
from __future__ import annotations


def install(log):
    from bot import score as scoring
    from bot.kucoin import PAPER_TRADE

    if getattr(scoring, "_paper_e2e_shadow_patched", False):
        return

    original_calculate = scoring.calculate

    async def calculate_with_paper_shadow(*args, **kwargs):
        result = await original_calculate(*args, **kwargs)
        if not isinstance(result, dict):
            return result

        if PAPER_TRADE and result.get("aprovado") is not True:
            out = dict(result)
            out["paper_shadow_original_approved"] = False
            out["paper_shadow_original_total"] = result.get("total")
            out["aprovado"] = True
            symbol = args[0] if args else kwargs.get("symbol", "?")
            direction = args[1] if len(args) > 1 else kwargs.get("direction", "?")
            log.info(
                "[PAPER_E2E] %s %s pre-trade=%s/%s seria BLOQUEADO em LIVE; "
                "shadow-only no PAPER para validar lifecycle completo",
                symbol,
                direction,
                result.get("total", "?"),
                getattr(scoring, "MIN_SCORE", "?"),
            )
            return out

        return result

    scoring.calculate = calculate_with_paper_shadow
    scoring._paper_e2e_shadow_patched = True
    if PAPER_TRADE:
        log.info("🧪 PAPER E2E: pre-trade scorer em shadow-only; LIVE permanece fail-closed")
