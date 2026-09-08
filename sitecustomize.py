"""NEXUS-7 runtime bootstrap.

Loaded implicitly by Python's site module. Concrete transitional overlays are
centralized in bot.runtime_overlays; this file only orchestrates installation.
"""
import builtins
import sys

builtins._nexus_sitecustomize_status = "installing"
try:
    from bot import shadow_startup_logging as _shadow_startup_logging
    _shadow_startup_logging.install_preimport()
    from bot.engine import TradingEngine
    from bot.strategy import Analyzer
    from bot import nexus_persistence as _np, funnel_metrics as _fm, mtf_shadow as _ms
    from bot import runtime_hardening as _rh, runtime_overlays as _runtime_overlays
    from bot import paper_e2e as _paper_e2e, paper_lifecycle as _paper_lifecycle
    from bot import paper_wallet as _paper_wallet, paper_validation_reset as _paper_validation_reset
    from bot import pretrade_hardening as _pretrade_hardening, rr_precision_hardening as _rr_precision_hardening
    from bot import scan_summary_hardening as _scan_summary_hardening, validation_safety_lock as _validation_safety_lock
    from bot import liquidation_override_guard as _liquidation_override_guard, instrument_readiness_guard as _instrument_readiness_guard
    from bot import nexus_decision_dedupe as _nexus_decision_dedupe, nexus_grade_display as _nexus_grade_display
    from bot import nexus_zero_observability as _nexus_zero_observability, daily_stop_observability as _daily_stop_observability
    from bot import daily_stop_runtime_hardening as _daily_stop_runtime_hardening, selfcheck_entrypoint_hardening as _selfcheck_entrypoint_hardening
    from bot import selfcheck as _selfcheck, news_context_hardening as _news_context_hardening
    from bot import balance_observability as _balance_observability, account_balance_observability as _account_balance_observability
    from bot import stagnation_time_hardening as _stagnation_time_hardening, pilot_readiness_observability as _pilot_readiness_observability
    from bot import shadow_mode_observability as _shadow_mode_observability, prelive_protection_failclosed as _prelive_protection_failclosed
    from bot import pilot_external_position_guard as _pilot_external_position_guard, kucoin as _kucoin, notifier as _notifier
    from bot.logger import log as _log

    _rh.install_database_schema_fix(_log); _rh.install_telegram_fix(_log); _rh.install_paper_execution_fix(_log)
    _pretrade_hardening.install(_log); _rr_precision_hardening.install(_log); _scan_summary_hardening.install(_log)
    _daily_stop_observability.install(_log); _daily_stop_runtime_hardening.install(TradingEngine, _log)
    _selfcheck_entrypoint_hardening.install(_selfcheck, _log); _paper_e2e.install(_log); _paper_wallet.install(_log)
    _paper_lifecycle.install(_log); _paper_validation_reset.install(_log)
    _prelive_protection_failclosed.install(TradingEngine, _kucoin, _log); _pilot_external_position_guard.install(TradingEngine, _log)
    _validation_safety_lock.install(_log); _liquidation_override_guard.install(_log); _instrument_readiness_guard.install(_log)
    _news_context_hardening.install(_log); _balance_observability.install(_log); _account_balance_observability.install(_log)
    _stagnation_time_hardening.install(TradingEngine, _log); _pilot_readiness_observability.install(_log)
    _shadow_mode_observability.install(_log); _nexus_decision_dedupe.install(_log); _nexus_grade_display.install(_notifier, _log)

    _runtime_overlays.install(
        TradingEngine=TradingEngine, Analyzer=Analyzer, nexus_persistence=_np,
        funnel_metrics=_fm, mtf_shadow=_ms,
        nexus_zero_observability=_nexus_zero_observability,
        nexus_decision_dedupe=_nexus_decision_dedupe,
        notifier=_notifier, log=_log,
    )
    builtins._nexus_sitecustomize_status = "ok"
except Exception as _sitecustomize_error:
    builtins._nexus_sitecustomize_status = "failed"
    print("CRITICAL: NEXUS sitecustomize hardening installation failed: "
          f"{type(_sitecustomize_error).__name__}: {_sitecustomize_error}", file=sys.stderr)
