"""Final runtime compatibility installer.

Most execution/decision hardenings are installed explicitly by
``bot.runtime_bootstrap``. This final layer keeps passive funnel observability,
installs the definitive headline-semantic classifier, and activates the narrow
NEXUS HTF-regime transition consistency guard after the rest of the NEXUS
wrapper stack has been composed.
"""
from __future__ import annotations


def install(TradingEngine, log) -> None:
    """Install passive observability plus final decision-semantics overlays."""
    from bot import funnel_metrics as fm
    from bot import score as scoring
    from bot import derivatives_news_freshness_hardening as derivatives_hardening
    from bot import news_semantic_hardening as news_semantics
    from bot import nexus_ai
    from bot import nexus_regime_transition_consistency as regime_transition

    fm.install(log)
    news_semantics.install(scoring, derivatives_hardening, log)

    # Install last so it observes the already-composed closed-candle/HTF regime
    # semantics and cost-calibrated NEXUS decision path. It does not alter any
    # class method, threshold, leverage, risk sizing, exchange permission or
    # order routing.
    regime_transition.install(nexus_ai, log)

    log.info(
        "[RUNTIME_OVERLAYS] no class monkey patches remain; passive funnel "
        "observability, final headline semantics and strict NEXUS "
        "regime-transition consistency are active; thresholds_unchanged=true "
        "leverage_unchanged=true"
    )
