"""Compatibility installer for remaining non-class runtime observability.

All TradingEngine/Analyzer method replacement overlays have been migrated to
explicit core/runtime composition. This module now installs only the passive
funnel log handler and performs no class or method assignment.
"""
from __future__ import annotations


def install(TradingEngine, log) -> None:
    """Install passive funnel observability; TradingEngine is kept for API compatibility."""
    from bot import funnel_metrics as fm

    fm.install(log)
    log.info(
        "[RUNTIME_OVERLAYS] no class monkey patches remain; "
        "decision_effect=NONE execution_effect=NONE"
    )
