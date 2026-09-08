"""Read-only status enrichment for NEXUS-7 API responses.

Keeps observability metrics outside ``TradingEngine.get_status`` so runtime
bootstrap no longer needs to monkey-patch the engine status method. This module
never changes strategy decisions, risk state, exchange state, or execution
permission.
"""
from __future__ import annotations

from bot.logger import log


def enrich_status(engine, base_status):
    """Return a copy of ``base_status`` enriched with read-only diagnostics.

    Best-effort by design: an observability failure leaves the canonical engine
    status intact and can never change execution behavior.
    """
    if not isinstance(base_status, dict):
        return base_status

    out = dict(base_status)
    try:
        from bot import funnel_metrics as fm
        from bot import mtf_shadow as ms
        from bot import nexus_decision_dedupe
        from bot import nexus_persistence as np

        out["nexus_persistent_metrics"] = np.get_cached_metrics()
        out["funnel_metrics"] = fm.get_funnel_metrics()
        out["mtf_shadow_metrics"] = ms.snapshot()
        out["nexus_dedupe_metrics"] = nexus_decision_dedupe.snapshot()

        if getattr(engine, "paper_trade", False):
            out["paper_wallet"] = {
                "balance": round(
                    float(
                        getattr(
                            engine,
                            "_paper_balance",
                            engine.risk.balance,
                        ) or 0.0
                    ),
                    4,
                ),
                "drawdown_pct": round(float(engine.risk.drawdown) * 100.0, 2),
                "isolated_from_exchange": True,
            }
    except Exception as exc:
        log.debug(
            "[STATUS_OBSERVABILITY] best_effort_failed error=%s "
            "decision_effect=NONE execution_effect=NONE",
            type(exc).__name__,
        )
    return out
