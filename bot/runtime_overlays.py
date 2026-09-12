"""Final runtime compatibility installer.

Most execution/decision hardenings are installed explicitly by
``bot.runtime_bootstrap``. This final layer keeps passive funnel observability,
installs the definitive headline-semantic classifier, activates the narrow
NEXUS HTF-regime transition consistency guard, and applies the stop-only CROSS
geometry target policy.

Runtime safety/execution class hardenings are installed by the canonical
bootstrap before this compatibility layer; no class monkey patches remain here.
"""
from __future__ import annotations


def install(TradingEngine, log) -> None:
    """Install passive observability plus final decision semantics."""
    del TradingEngine
    from bot import funnel_metrics as fm
    from bot import score as scoring
    from bot import derivatives_news_freshness_hardening as derivatives_hardening
    from bot import news_semantic_hardening as news_semantics
    from bot import nexus_ai
    from bot import nexus_regime_transition_consistency as regime_transition
    from bot import kucoin_contract_risk_hardening as cross_risk_hardening
    from bot import cross_geometry_target_policy as cross_target_policy

    fm.install(log)
    news_semantics.install(scoring, derivatives_hardening, log)

    # The core CROSS-risk hardening is already installed by runtime_bootstrap.
    # This policy replaces only its module-level geometry helper: class APIs,
    # exchange routing, leverage, thresholds and sizing authorities are untouched.
    cross_target_policy.install(cross_risk_hardening, log)

    # Install last so it observes the already-composed closed-candle/HTF regime
    # semantics and cost-calibrated NEXUS decision path. It does not alter any
    # class method, threshold, leverage, exchange permission or order routing.
    regime_transition.install(nexus_ai, log)

    log.info(
        "[RUNTIME_OVERLAYS] passive funnel observability, final headline semantics, "
        "stop-only CROSS target policy and strict NEXUS regime-transition "
        "consistency are active; thresholds_unchanged=true leverage_unchanged=true "
        "railway_variables_unchanged=true"
    )
