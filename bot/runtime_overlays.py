"""Transitional runtime overlays for NEXUS-7.

This module owns the remaining observability/infrastructure wrappers that are
not yet implemented directly in their core classes. Keeping them here makes
``runtime_bootstrap`` declarative and gives each overlay one explicit migration
boundary for later removal.

No strategy threshold, risk rule, release state, exchange mutation, or
execution permission is changed here.
"""
from __future__ import annotations

import asyncio


def install(TradingEngine, log) -> None:
    """Install the remaining legacy-compatible engine overlay exactly once."""
    from bot import funnel_metrics as fm
    from bot import nexus_persistence as np
    from bot import nexus_zero_observability
    from bot import notifier

    # NEXUS persistence I/O serialization now lives in nexus_persistence core.
    # MTF shadow observation now lives at the Analyzer method definition.
    fm.install(log)

    if not getattr(TradingEngine, "_nexus_persistence_patched", False):
        orig_validate = TradingEngine._nexus_validate

        async def validate_with_history(self, sig):
            dec = await orig_validate(self, sig)
            nexus_zero_observability.observe(dec, log)
            try:
                await np.record_decision(sig, dec)
                asyncio.create_task(np.evaluate_pending(self.client))
            except Exception as exc:
                log.debug(
                    "[NEXUS_PERSISTENCE] best_effort_failed error=%s "
                    "decision_effect=NONE execution_effect=NONE",
                    type(exc).__name__,
                )
            try:
                if getattr(dec, "execution_allowed", False) is not True:
                    asyncio.create_task(
                        notifier.notify_nexus(dec.to_dict(), approved=False)
                    )
            except Exception as exc:
                log.debug(
                    "[NEXUS_NOTIFY] best_effort_schedule_failed error=%s "
                    "decision_effect=NONE execution_effect=NONE",
                    type(exc).__name__,
                )
            return dec

        TradingEngine._nexus_validate = validate_with_history
        TradingEngine._nexus_persistence_patched = True

    log.info(
        "[RUNTIME_OVERLAYS] installed transitional overlays; "
        "decision_effect=NONE execution_effect=NONE"
    )
