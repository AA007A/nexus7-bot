"""Maintained list of paths whose changes can alter trading decisions.

Any change to these files can change which candidates are taken, their size,
their exits, or their cost accounting, so ordinary unit tests alone are not
sufficient evidence. ``.github/workflows/oos_real_replay.yml`` must trigger on
every pattern listed here; ``tests/test_decision_affecting_paths.py`` fails
if the workflow filter drifts from this list.

Patterns use GitHub Actions ``paths`` glob syntax. Keep them sorted by domain.
Adding a new decision-affecting module means adding it here in the same PR.
"""

DECISION_AFFECTING_PATHS: tuple[str, ...] = (
    # Signal generation / strategy gates
    "bot/strategy.py",
    "bot/indicators.py",
    "bot/score.py",
    "bot/score_weights.py",
    "bot/scoring_safety_hardening.py",
    "bot/adaptive_mtf_entry.py",
    "bot/adaptive_mtf_calibration.py",
    "bot/pullback_confirmation_hardening.py",
    "bot/pretrade_hardening.py",
    "bot/rr_precision_hardening.py",
    "bot/rr_gate_calibration.py",
    "bot/correlation.py",
    "bot/event_intelligence.py",
    "bot/market_viability_fail_closed.py",
    # NEXUS ensemble, probability, calibration and OOS evidence
    "bot/nexus_*.py",
    "bot/oos_*.py",
    # Market data semantics
    "bot/market_data.py",
    "bot/market_data_integrity.py",
    # External market-risk / news context that can veto entries
    "bot/market_risk_*.py",
    "bot/news_*.py",
    "bot/derivatives_news_freshness_hardening.py",
    # Risk, sizing and loss budgets
    "bot/config.py",
    "bot/risk.py",
    "bot/risk_manager_v3.py",
    "bot/risk_policy.py",
    "bot/professional_risk.py",
    "bot/professional_risk_adapter.py",
    "bot/final_sizing_invariants.py",
    "bot/final_loss_budget.py",
    "bot/operator_runtime_policy.py",
    "bot/operator_loss_policy.py",
    "bot/pilot_risk_cap_hardening.py",
    "bot/quantity.py",
    "bot/cross_portfolio_stress.py",
    "bot/cross_geometry_target_policy.py",
    "bot/kucoin_contract_risk_hardening.py",
    "bot/liquidation.py",
    "bot/liquidation_override_guard.py",
    "bot/daily_stop_*.py",
    "bot/daily_tracker.py",
    # Exits
    "bot/confirmed_rr_exit.py",
    "bot/trailing_safety_hardening.py",
    "bot/stagnation_time_hardening.py",
    "bot/partial_tp_execution_hardening.py",
    "bot/exit_policy_telemetry.py",
    # Cost modelling and replay/backtest semantics
    "bot/kucoin_execution_model.py",
    "bot/backtest.py",
)
