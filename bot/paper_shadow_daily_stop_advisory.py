"""PAPER/SHADOW-only daily-stop advisory.

This overlay exists solely to keep simulated/read-only validation pipelines
running after the daily stop would have fired.  Controlled LIVE trading is
unchanged: its daily stop remains a hard circuit breaker.

The overlay does not alter thresholds, leverage, sizing, NEXUS decisions,
position management, or any exchange mutation path.
"""


def install(TradingEngine, log):
    if getattr(TradingEngine, "_paper_shadow_daily_stop_advisory", False):
        return

    original_update_daily_pnl = TradingEngine._update_daily_pnl

    def _is_simulation_or_shadow(engine) -> bool:
        # PAPER is explicit on the engine. SHADOW is the validation safety-lock
        # read-only pipeline. A controlled LIVE pilot has neither condition.
        return bool(
            getattr(engine, "paper_trade", False)
            or getattr(engine, "_validation_safety_lock_active", False)
        )

    def _update_daily_pnl_advisory(self, *args, **kwargs):
        result = original_update_daily_pnl(self, *args, **kwargs)

        if not _is_simulation_or_shadow(self):
            return result

        tracker = getattr(self, "daily_tracker", None)
        stop_was_active = bool(
            getattr(self, "daily_stopped", False)
            or (tracker is not None and getattr(tracker, "daily_stopped", False))
        )
        if not stop_was_active:
            return result

        # Clear only the DAILY entry circuit breaker in non-executing modes.
        # Weekly/monthly tracker state is deliberately untouched.
        self.daily_stopped = False
        if tracker is not None:
            tracker.daily_stopped = False

        log.warning(
            "[PAPER_SHADOW_DAILY_STOP_ADVISORY] mode=%s daily_stop_triggered=true "
            "entries_blocked=false live_policy_unchanged=true execution_effect=NONE",
            "PAPER" if getattr(self, "paper_trade", False) else "SHADOW",
        )
        return result

    TradingEngine._update_daily_pnl = _update_daily_pnl_advisory
    TradingEngine._paper_shadow_daily_stop_advisory = True
    log.info(
        "[PAPER_SHADOW_DAILY_STOP_ADVISORY] installed paper_shadow_only=true "
        "live_daily_stop_hard=true"
    )
