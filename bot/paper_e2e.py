"""PAPER-only E2E helper.

Keeps production decision systems fully active for diagnostics, but in PAPER
mode selected post-NEXUS blockers can be shadow-only so the simulator can
exercise the complete candidate -> NEXUS -> sizing -> order -> management ->
close lifecycle.

Live trading is untouched: the same pre-trade and liquidation rejections stay
blocking. This module never changes PAPER_TRADE, live confirmations, leverage,
sizing, NEXUS thresholds, integrity gates, or exchange order code.
"""
from __future__ import annotations

import copy


def install(log):
    from bot import score as scoring
    from bot import liquidation as liq
    from bot.kucoin import PAPER_TRADE

    if getattr(scoring, "_paper_e2e_shadow_patched", False):
        return

    original_calculate = scoring.calculate
    original_liq_analyze = liq.analyze

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

    def liquidation_with_paper_shadow(*args, **kwargs):
        result = original_liq_analyze(*args, **kwargs)
        if PAPER_TRADE and getattr(result, "stop_effective", True) is not True:
            out = copy.copy(result)
            out.stop_effective = True
            out.reason = (
                f"PAPER_E2E shadow-only; LIVE bloquearia: {getattr(result, 'reason', '')}"
            )
            symbol = kwargs.get("symbol") or getattr(result, "symbol", "?")
            log.info(
                "[PAPER_E2E] %s stop %.2f%% vs liq %.2f%% seria BLOQUEADO em LIVE; "
                "shadow-only no PAPER para validar lifecycle completo",
                symbol,
                getattr(result, "stop_move_pct", 0.0),
                getattr(result, "liq_move_pct", 0.0),
            )
            return out
        return result

    scoring.calculate = calculate_with_paper_shadow
    scoring._paper_e2e_shadow_patched = True
    liq.analyze = liquidation_with_paper_shadow
    liq._paper_e2e_shadow_patched = True

    if PAPER_TRADE:
        log.info(
            "🧪 PAPER E2E: pre-trade e liquidation gates em shadow-only; "
            "LIVE permanece fail-closed"
        )
