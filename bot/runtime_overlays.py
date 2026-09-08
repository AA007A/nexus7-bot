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


def install(TradingEngine, Analyzer, log) -> None:
    """Install legacy-compatible overlays exactly once per owning component."""
    from bot import funnel_metrics as fm
    from bot import mtf_shadow as ms
    from bot import nexus_decision_dedupe
    from bot import nexus_persistence as np
    from bot import nexus_zero_observability
    from bot import notifier

    if not getattr(np, "_single_conn_serialized", False):
        orig_execute = np._execute
        orig_fetchall = np._fetchall
        io_lock = asyncio.Lock()

        async def execute_serialized(sql, params=()):
            async with io_lock:
                return await orig_execute(sql, params)

        async def fetchall_serialized(sql, params=()):
            async with io_lock:
                return await orig_fetchall(sql, params)

        np._execute = execute_serialized
        np._fetchall = fetchall_serialized
        np._single_conn_serialized = True

    fm.install(log)

    if not getattr(Analyzer, "_mtf_shadow_patched", False):
        orig_analyze_mtf = Analyzer.analyze_mtf

        def analyze_mtf_with_shadow(self, symbol, k15, k1h, k4h,
                                    min_score=60, fee_mult=2.0, vol_mult=1.0):
            result = orig_analyze_mtf(
                self, symbol, k15, k1h, k4h,
                min_score=min_score,
                fee_mult=fee_mult,
                vol_mult=vol_mult,
            )
            try:
                before = ms.snapshot().get("unique_states", 0)
                ms.observe(
                    symbol, k15, k1h, k4h,
                    production_result=result,
                    min_score=min_score,
                    fee_mult=fee_mult,
                    vol_mult=vol_mult,
                )
                snap = ms.snapshot()
                unique = snap.get("unique_states", 0)
                if unique != before and (unique == 1 or unique % 25 == 0):
                    log.info(
                        "[MTF_SHADOW] unique=%s eligible=%s survivors=%s "
                        "nexus_approved=%s nexus_vetoed=%s execution_effect=NONE",
                        unique,
                        snap.get("eligible_4h_dir_1h_neutral", 0),
                        snap.get("shadow_pre_ai_survivors", 0),
                        snap.get("shadow_nexus_approved", 0),
                        snap.get("shadow_nexus_vetoed", 0),
                    )
            except Exception as exc:
                log.debug(
                    "[MTF_SHADOW] observability_failed error=%s "
                    "decision_effect=NONE execution_effect=NONE",
                    type(exc).__name__,
                )
            return result

        Analyzer.analyze_mtf = analyze_mtf_with_shadow
        Analyzer._mtf_shadow_patched = True

    if not getattr(TradingEngine, "_nexus_persistence_patched", False):
        orig_validate = TradingEngine._nexus_validate
        orig_status = getattr(TradingEngine, "get_status", None)

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

        if orig_status is not None:
            def status_with_nexus_metrics(self, *args, **kwargs):
                out = orig_status(self, *args, **kwargs)
                try:
                    if isinstance(out, dict):
                        out = dict(out)
                        out["nexus_persistent_metrics"] = np.get_cached_metrics()
                        out["funnel_metrics"] = fm.get_funnel_metrics()
                        out["mtf_shadow_metrics"] = ms.snapshot()
                        out["nexus_dedupe_metrics"] = nexus_decision_dedupe.snapshot()
                        if getattr(self, "paper_trade", False):
                            out["paper_wallet"] = {
                                "balance": round(
                                    float(
                                        getattr(
                                            self,
                                            "_paper_balance",
                                            self.risk.balance,
                                        ) or 0.0
                                    ),
                                    4,
                                ),
                                "drawdown_pct": round(
                                    float(self.risk.drawdown) * 100.0,
                                    2,
                                ),
                                "isolated_from_exchange": True,
                            }
                except Exception as exc:
                    log.debug(
                        "[STATUS_OBSERVABILITY] best_effort_failed error=%s "
                        "decision_effect=NONE execution_effect=NONE",
                        type(exc).__name__,
                    )
                return out

            TradingEngine.get_status = status_with_nexus_metrics

        TradingEngine._nexus_persistence_patched = True

    log.info(
        "[RUNTIME_OVERLAYS] installed transitional overlays; "
        "decision_effect=NONE execution_effect=NONE"
    )
