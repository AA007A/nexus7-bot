"""Observational NEXUS confidence capture.

Transitional instrumentation: wraps TradingEngine._nexus_validate only to persist
calibration evidence after the original decision returns. The returned decision
object is passed through unchanged. No score, threshold, risk or execution path
is modified.
"""
from __future__ import annotations


def install(TradingEngine, log):
    if getattr(TradingEngine, "_nexus_confidence_observability_installed", False):
        return

    original = TradingEngine._nexus_validate

    async def wrapped(self, sig, *args, **kwargs):
        nx_dec = await original(self, sig, *args, **kwargs)
        try:
            from bot import database as db
            from bot.nexus_confidence_evidence import capture_decision

            mode = "PAPER" if getattr(self, "paper_trade", False) else (
                "SHADOW" if getattr(self, "_validation_safety_lock_active", False) else "LIVE"
            )
            ok = await capture_decision(
                db,
                nx_dec=nx_dec,
                proposed_side=getattr(sig, "direction", ""),
                decision_source="nexus_ai",
                mode=mode,
            )
            log.info(
                "[NEXUS_CONFIDENCE_EVIDENCE] symbol=%s confidence=%s expected_value=%s "
                "approved=%s mode=%s persisted=%s decision_effect=NONE execution_effect=NONE",
                getattr(nx_dec, "symbol", getattr(sig, "symbol", "?")),
                getattr(nx_dec, "confidence", "N/A"),
                getattr(nx_dec, "expected_value", "N/A"),
                getattr(nx_dec, "execution_allowed", False) is True,
                mode,
                ok,
            )
        except Exception as exc:
            log.warning(
                "[NEXUS_CONFIDENCE_EVIDENCE] capture_failed symbol=%s error=%s "
                "decision_effect=NONE execution_effect=NONE",
                getattr(sig, "symbol", "?"), type(exc).__name__,
            )

        # Read-only PAPER outcome calibration report, throttled independently of
        # decision capture. It never feeds back into NEXUS or runtime thresholds.
        try:
            from bot import database as db
            from bot.oos_calibration_readiness import maybe_log_readiness
            await maybe_log_readiness(db, log)
        except Exception as exc:
            log.warning(
                "[NEXUS_OOS_CALIBRATION] observability_failed error=%s "
                "decision_effect=NONE execution_effect=NONE",
                type(exc).__name__,
            )
        return nx_dec

    TradingEngine._nexus_validate = wrapped
    TradingEngine._nexus_confidence_observability_installed = True

    # Install outcome linkage only for PAPER persistence. LIVE/SHADOW remain
    # untouched because real fills may shift entry/stop geometry after the
    # decision; LIVE linkage should later propagate an explicit evidence_id.
    try:
        from bot import database as db
        from bot import confidence_outcome_runtime
        confidence_outcome_runtime.install(db, log)
    except Exception as exc:
        log.warning(
            "[CONFIDENCE_OUTCOME_LINK] PAPER hook install failed error=%s "
            "decision_effect=NONE execution_effect=NONE",
            type(exc).__name__,
        )

    log.info(
        "[NEXUS_CONFIDENCE_EVIDENCE] installed observational-only; "
        "decision_effect=NONE execution_effect=NONE"
    )
