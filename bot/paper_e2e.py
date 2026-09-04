"""PAPER-only E2E helper.

Keeps production decision systems fully active for diagnostics, but in PAPER
mode selected post-NEXUS blockers can be shadow-only so the simulator can
exercise the complete candidate -> NEXUS -> sizing -> order -> management ->
close lifecycle.

Live trading is untouched: the same pre-trade, liquidation and balance rules
stay blocking. In PAPER only, sizing is capped to a fee-aware affordable
quantity so an intentionally aggressive test configuration can still exercise
the simulator without pretending funds exist.
"""
from __future__ import annotations

import copy


def install(log):
    from bot import score as scoring
    from bot import liquidation as liq
    from bot.risk import RiskManager
    from bot.config import cfg
    from bot.kucoin import PAPER_TRADE, TAKER_FEE

    if getattr(scoring, "_paper_e2e_shadow_patched", False):
        return

    original_calculate = scoring.calculate
    original_liq_analyze = liq.analyze
    original_size = RiskManager.size

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

    def size_with_paper_fee_cap(self, symbol, entry, instruments,
                                size_mult=1.0, open_positions=None):
        qty = original_size(
            self, symbol, entry, instruments,
            size_mult=size_mult, open_positions=open_positions,
        )
        if not PAPER_TRADE or qty <= 0 or entry <= 0 or self.balance <= 0:
            return qty

        # The production RiskManager caps margin, but with an aggressive 98%
        # margin cap the opening taker fee can make required cash exceed the
        # current balance. For PAPER E2E, cap to the largest genuinely
        # affordable quantity instead of bypassing the balance gate.
        per_unit_cash = entry * (1.0 / cfg.LEVERAGE + TAKER_FEE)
        if per_unit_cash <= 0:
            return qty
        max_affordable = self.balance / per_unit_cash
        if qty <= max_affordable:
            return qty

        info = instruments.get(symbol, {}) or {}
        multiplier = float(info.get("multiplier", 1) or 1)
        lot = float(info.get("minQty", 1) or 1)
        step = multiplier * lot
        if step > 0:
            from decimal import Decimal, ROUND_FLOOR
            q = Decimal(str(max_affordable))
            s = Decimal(str(step))
            max_affordable = float(
                (q / s).to_integral_value(rounding=ROUND_FLOOR) * s
            )

        capped = max(0.0, min(qty, max_affordable))
        log.info(
            "[PAPER_E2E] %s sizing cap fee-aware: %.8f -> %.8f "
            "(saldo=%.4f, LIVE sizing inalterado)",
            symbol, qty, capped, self.balance,
        )
        return capped

    scoring.calculate = calculate_with_paper_shadow
    scoring._paper_e2e_shadow_patched = True
    liq.analyze = liquidation_with_paper_shadow
    liq._paper_e2e_shadow_patched = True
    RiskManager.size = size_with_paper_fee_cap
    RiskManager._paper_e2e_size_patched = True

    if PAPER_TRADE:
        log.info(
            "🧪 PAPER E2E: pre-trade/liquidation shadow-only + sizing fee-aware; "
            "LIVE permanece fail-closed"
        )
