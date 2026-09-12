"""Final runtime compatibility installer.

Most execution/decision hardenings are installed explicitly by
``bot.runtime_bootstrap``. This final layer keeps passive funnel observability,
installs the definitive headline-semantic classifier, activates the narrow
NEXUS HTF-regime transition consistency guard, applies the stop-only CROSS
geometry target policy, and delegates operator-requested LIVE policies to their
isolated hardening modules.

The overlay itself contains no direct class assignments; no class monkey patches remain
in this compatibility module. Class hardening implementation details stay isolated
behind explicit ``install`` calls, consistent with the existing bootstrap pattern.
"""
from __future__ import annotations


def install(TradingEngine, log) -> None:
    """Install passive observability plus final delegated policy semantics."""
    from bot import funnel_metrics as fm
    from bot import score as scoring
    from bot import strategy
    from bot import engine as core_engine
    from bot import derivatives_news_freshness_hardening as derivatives_hardening
    from bot import news_semantic_hardening as news_semantics
    from bot import nexus_ai
    from bot import nexus_regime_transition_consistency as regime_transition
    from bot import kucoin_contract_risk_hardening as cross_risk_hardening
    from bot import cross_geometry_target_policy as cross_target_policy
    from bot import operator_runtime_policy
    from bot import exit_policy_telemetry
    from bot import operator_loss_policy
    from bot import entry_type_shadow_overlay
    from bot import scoring_safety_hardening
    from bot import trailing_safety_hardening

    fm.install(log)
    news_semantics.install(scoring, derivatives_hardening, log)

    # Correct score semantics before any downstream analyzer/shadow consumes
    # score_tf. Thresholds remain exactly unchanged.
    scoring_safety_hardening.install(strategy, log)

    # Correct trailing geometry on the canonical Position class. This changes
    # only the trailing calculation; native exchange SL/TP remains authoritative.
    trailing_safety_hardening.install(core_engine.Position, strategy.cfg, log)

    # Observe the fully composed Adaptive analyzer without changing its return.
    # This measures possible false PULLBACK classifications only after LIVE logic
    # has already decided HOLD.
    entry_type_shadow_overlay.install(strategy.Analyzer, strategy, log)

    # The core CROSS-risk hardening is already installed by runtime_bootstrap.
    # This policy replaces only its module-level geometry helper: class APIs,
    # exchange routing, leverage, thresholds and sizing authorities are untouched.
    cross_target_policy.install(cross_risk_hardening, log)

    # Delegated modules own these runtime hardenings. This file does not directly
    # assign TradingEngine/RiskManager methods or mutate exchange state.
    operator_runtime_policy.install(TradingEngine, log)
    exit_policy_telemetry.install(TradingEngine, log)
    operator_loss_policy.install(TradingEngine, log)

    # Install last so it observes the already-composed closed-candle/HTF regime
    # semantics and cost-calibrated NEXUS decision path. It does not alter any
    # class method, threshold, leverage, exchange permission or order routing.
    regime_transition.install(nexus_ai, log)

    log.info(
        "[RUNTIME_OVERLAYS] passive funnel observability, scoring/trailing safety, "
        "entry-type shadow diagnostics, final headline semantics, stop-only CROSS "
        "target policy, delegated operator margin/drawdown/exit policy and strict "
        "NEXUS regime-transition consistency are active; thresholds_unchanged=true "
        "leverage_unchanged=true railway_variables_unchanged=true"
    )
