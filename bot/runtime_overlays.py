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
    from bot import cross_portfolio_stress
    from bot import operator_runtime_policy
    from bot import exit_policy_telemetry
    from bot import operator_loss_policy
    from bot import entry_type_shadow_overlay
    from bot import scoring_safety_hardening
    from bot import trailing_safety_hardening
    from bot import market_data_integrity
    from bot import volume_ratio_diagnostics
    from bot import market_viability_fail_closed
    from bot import restart_opening_order_lineage
    from bot import post_trade_forensics
    from bot import daily_pnl_estimate_lineage_hardening
    from bot import durable_daily_pnl
    from bot import order_visibility_race_hardening
    from bot import policy_log_throttle
    from bot import partial_tp_execution_hardening
    from bot import exchange_accounting_evidence
    from bot import daily_pnl_exchange_reconciliation
    from bot import daily_stop_override_telemetry
    from bot import external_origin_runtime
    from bot import market_risk_runtime
    from bot import market_risk_coverage_observability
    from bot import entry_latency_observability
    from bot import runtime_contract_guard
    from bot.pilot import PilotGuard
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

    # The daily-stop override layer owns authorization semantics. This wrapper
    # changes only the user-facing notification so it cannot falsely claim
    # entries are blocked while an explicit override is active.
    daily_stop_override_telemetry.install(core_engine, log)

    # Make external-provider degradation explicit without changing the existing
    # fail-neutral execution policy, sizing, leverage or entry authorization.
    market_risk_coverage_observability.install(market_risk_runtime, log)

    # Observe the already-emitted runtime log stream to timestamp the entry
    # funnel. No execution-critical callable is wrapped or mutated.
    entry_latency_observability.install(log)

    # A position recovered after process restart already has exact durable +
    # exchange ownership proof. Rehydrate that proof's opening order id into the
    # Position accounting lineage before any later close/checkpoint can occur.
    # Missing/conflicting evidence is never invented or overwritten.
    restart_opening_order_lineage.install(TradingEngine, log)

    post_trade_forensics.install(
        TradingEngine, core_engine.Position, strategy.cfg, TAKER_FEE, log
    )

    # Any engine-owned direct close estimate must be classified and enriched
    # before its first durable ledger write. In particular, RR_DOUBLE can close
    # and remove a Position without passing through _sync_positions; preserve
    # exact opening-order lineage there as accounting metadata only.
    daily_pnl_estimate_lineage_hardening.install(durable_daily_pnl, log)

    # Reconcile conservative operational estimates to KuCoin-confirmed realized
    # PnL only after the existing accounting audit proves BGX IDs + fills +
    # durable lineage. Risk remains conservative while evidence is pending.
    daily_pnl_exchange_reconciliation.install(exchange_accounting_evidence, log)

    # Persist positive live EXTERNAL/read-only ownership observations and allow
    # post-trade accounting to consume them only when no BGX order evidence
    # conflicts. Telemetry only: no execution/risk authority is introduced.
    external_origin_runtime.install(TradingEngine, exchange_accounting_evidence, log)

    regime_transition.install(nexus_ai, log)

    # The legacy CROSS guard remains fail-closed unless this module is present.
    # With one existing pilot position, exact authorization is deferred until
    # final quantity exists and then requires a simultaneous-stop portfolio
    # stress projection below 90% account risk. Missing/inconsistent private
    # exchange state, non-CROSS positions, or additional non-reduce orders block.
    cross_portfolio_stress.install(TradingEngine, log)

    # MUST remain the final installer in this compatibility stack. It verifies
    # that execution-critical callables are still owned by the reviewed final
    # wrappers and fails startup if a later patch silently replaces one.
    runtime_contract_guard.install(
        TradingEngine, PilotGuard, nexus_ai, core_engine, log
    )

    log.info(
        "[RUNTIME_OVERLAYS] passive funnel and entry-latency observability, "
        "scoring/trailing safety, entry-type shadow diagnostics, volume-ratio diagnostics, "
        "fail-closed market viability, KuCoin order-visibility race hardening, fail-closed "
        "partial-TP execution, drawdown advisory log throttling, truthful daily-stop override "
        "telemetry, market-risk coverage telemetry, restart opening-order lineage recovery, "
        "post-trade forensics, direct-close daily-PnL estimate lineage, confirmed daily-PnL "
        "reconciliation, durable external-origin telemetry, final headline semantics, stop-only "
        "CROSS target policy, fail-closed CROSS portfolio stop-stress gate, delegated operator "
        "margin/drawdown/exit policy, strict NEXUS regime-transition consistency and final "
        "runtime ownership contract are active; thresholds_unchanged=true leverage_unchanged=true "
        "railway_variables_unchanged=true"
    )
