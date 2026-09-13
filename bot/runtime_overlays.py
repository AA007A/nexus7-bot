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
    from bot import market_data_integrity
    from bot import volume_ratio_diagnostics
    from bot import market_viability_fail_closed
    from bot import post_trade_forensics
    from bot import order_visibility_race_hardening
    from bot import policy_log_throttle
    from bot import partial_tp_execution_hardening
    from bot import exchange_accounting_evidence
    from bot import external_origin_runtime
    from bot.kucoin import KuCoinClient, TAKER_FEE

    fm.install(log)
    news_semantics.install(scoring, derivatives_hardening, log)

    scoring_safety_hardening.install(strategy, log)
    trailing_safety_hardening.install(core_engine.Position, strategy.cfg, log)
    volume_ratio_diagnostics.install(strategy.Analyzer, market_data_integrity, log)
    entry_type_shadow_overlay.install(strategy.Analyzer, strategy, log)
    market_viability_fail_closed.install(TradingEngine, strategy.cfg, log)

    # Private WS may lead KuCoin REST indexing briefly. Delay only the first
    # authoritative REST confirmation within the existing timeout budget.
    order_visibility_race_hardening.install(KuCoinClient, log)

    # Partial exits are execution-critical: explicit sized reduce-only semantics,
    # REST fill proof, exchange quantity truth and fail-closed BE protection.
    partial_tp_execution_hardening.install(TradingEngine, KuCoinClient, TAKER_FEE, log)

    cross_target_policy.install(cross_risk_hardening, log)

    # Preserve the operator-requested drawdown semantics, but avoid two advisory
    # WARNINGs every scan cycle hiding operationally significant warnings.
    operator_runtime_policy.install(TradingEngine, policy_log_throttle.wrap(log))
    exit_policy_telemetry.install(TradingEngine, log)
    operator_loss_policy.install(TradingEngine, log)

    post_trade_forensics.install(
        TradingEngine, core_engine.Position, strategy.cfg, TAKER_FEE, log
    )

    # Persist positive live EXTERNAL/read-only ownership observations and allow
    # post-trade accounting to consume them only when no BGX order evidence
    # conflicts. Telemetry only: no execution/risk authority is introduced.
    external_origin_runtime.install(TradingEngine, exchange_accounting_evidence, log)

    regime_transition.install(nexus_ai, log)

    log.info(
        "[RUNTIME_OVERLAYS] passive funnel observability, scoring/trailing safety, "
        "entry-type shadow diagnostics, volume-ratio diagnostics, fail-closed market "
        "viability, KuCoin order-visibility race hardening, fail-closed partial-TP "
        "execution, drawdown advisory log throttling, post-trade forensics, durable "
        "external-origin telemetry, final headline semantics, stop-only CROSS target "
        "policy, delegated operator margin/drawdown/exit policy and strict NEXUS "
        "regime-transition consistency are active; thresholds_unchanged=true "
        "leverage_unchanged=true railway_variables_unchanged=true"
    )
