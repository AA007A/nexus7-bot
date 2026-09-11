"""Final runtime compatibility installer.

Most execution/decision hardenings are installed explicitly by
``bot.runtime_bootstrap``. This final layer keeps passive funnel observability,
installs the definitive headline-semantic classifier, activates the narrow
NEXUS HTF-regime transition consistency guard, and preserves the strategy's
original TP when 50x CROSS risk requires only the stop to be tightened.
"""
from __future__ import annotations


def install(TradingEngine, log) -> None:
    """Install final passive and decision/geometry consistency overlays."""
    from bot import funnel_metrics as fm
    from bot import score as scoring
    from bot import derivatives_news_freshness_hardening as derivatives_hardening
    from bot import news_semantic_hardening as news_semantics
    from bot import nexus_ai
    from bot import nexus_regime_transition_consistency as regime_transition
    from bot import kucoin_contract_risk_hardening as contract_risk
    from bot import cross_geometry_strategy_tp as strategy_tp

    fm.install(log)
    news_semantics.install(scoring, derivatives_hardening, log)

    # The CROSS module is installed earlier by runtime_bootstrap. Replace only
    # its pure geometry helper so existing wrappers keep the same execution and
    # mandatory NEXUS-recheck path while preserving the strategy TP.
    strategy_tp.install(contract_risk, log)

    # Install after the rest of the NEXUS wrapper stack so it observes the
    # already-composed closed-candle/HTF regime semantics and calibrated costs.
    regime_transition.install(nexus_ai, log)

    log.info(
        "[RUNTIME_OVERLAYS] passive funnel observability, final headline "
        "semantics, strategy-TP preservation and strict NEXUS regime-transition "
        "consistency are active; thresholds_unchanged=true leverage_unchanged=true "
        "sizing_unchanged=true"
    )
