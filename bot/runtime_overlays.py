"""Compatibility installer for final runtime observability and semantics.

Most execution/decision hardenings are installed explicitly by
``bot.runtime_bootstrap``. This final compatibility layer keeps passive funnel
observability and installs the definitive headline-semantic classifier after
all earlier news/context wrappers have been composed.
"""
from __future__ import annotations


def install(TradingEngine, log) -> None:
    """Install passive funnel metrics and final headline semantics."""
    from bot import funnel_metrics as fm
    from bot import score as scoring
    from bot import derivatives_news_freshness_hardening as derivatives_hardening
    from bot import news_semantic_hardening as news_semantics

    fm.install(log)
    news_semantics.install(scoring, derivatives_hardening, log)
    log.info(
        "[RUNTIME_OVERLAYS] no class monkey patches remain in this compatibility "
        "installer; passive funnel observability and final headline semantics "
        "are active; execution_effect=NONE"
    )
