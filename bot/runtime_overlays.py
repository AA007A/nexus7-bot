"""Compatibility installer for remaining passive runtime observability.

All legacy TradingEngine/Analyzer decision or execution method replacement
overlays have been migrated to explicit core/runtime composition. This module
installs only the passive funnel log handler. Notification-only instrumentation
may still wrap methods elsewhere without changing trading decisions or exchange
behavior.
"""
from __future__ import annotations


def install(TradingEngine, log) -> None:
    """Install passive funnel observability; TradingEngine is kept for API compatibility."""
    from bot import funnel_metrics as fm

    fm.install(log)
    log.info(
        "[RUNTIME_OVERLAYS] no class monkey patches remain; "
        "notification-only instrumentation may be active; "
        "decision_effect=NONE execution_effect=NONE"
    )
