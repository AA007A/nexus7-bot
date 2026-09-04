"""Runtime-only observability and infrastructure hardening for NEXUS AI.

Loaded automatically by Python's site module. It wraps decision validation,
engine status, and the Analyzer for shadow-only A/B measurement without
changing live trading mode or bypassing the mandatory NEXUS gate.
"""
import asyncio
import threading
import time

try:
    from bot.engine import TradingEngine
    from bot.strategy import Analyzer
    from bot import nexus_persistence as _np
    from bot import funnel_metrics as _fm
    from bot import mtf_shadow as _ms
    from bot import logger as _logger
    from bot import runtime_hardening as _rh
    from bot import paper_e2e as _paper_e2e
    from bot import paper_lifecycle as _paper_lifecycle
    from bot import paper_wallet as _paper_wallet
    from bot import pretrade_hardening as _pretrade_hardening
    from bot import rr_precision_hardening as _rr_precision_hardening
    from bot import scan_summary_hardening as _scan_summary_hardening
    from bot import validation_safety_lock as _validation_safety_lock
    from bot import silent_except_audit as _silent_except_audit
    from bot import nexus_decision_dedupe as _nexus_decision_dedupe
    from bot import notifier as _notifier
    from bot.logger import log as _log

    _rh.install_database_schema_fix(_log)
    _rh.install_telegram_fix(_log)
    _rh.install_paper_execution_fix(_log)
    _pretrade_hardening.install(_log)
    _rr_precision_hardening.install(_log)
    _scan_summary_hardening.install(_log)
    _paper_e2e.install(_log)
    _paper_wallet.install(_log)
    _paper_lifecycle.install(_log)
    _validation_safety_lock.install(_log)
    _silent_except_audit.audit_silent_excepts(_log)
    _nexus_decision_dedupe.install(_log)

    if not getattr(_np, "_single_conn_serialized", False):
        _np_orig_execute = _np._execute
        _np_orig_fetchall = _np._fetchall
        _np_io_lock = asyncio.Lock()

        async def _np_execute_serialized(sql, params=()):
            async with _np_io_lock:
                return await _np_orig_execute(sql, params)

        async def _np_fetchall_serialized(sql, params=()):
            async with _np_io_lock:
                return await _np_orig_fetchall(sql, params)

        _np._execute = _np_execute_serialized
        _np._fetchall = _np_fetchall_serialized
        _np._single_conn_serialized = True

    if not getattr(_logger, "_audit_pacing_patched", False):
        _orig_enqueue = _logger._enqueue
        _audit_lock = threading.Lock()
        _audit_last = [0.0]
        _audit_min_interval = 1.5

        def _paced_enqueue(text):
            now = time.monotonic()
            with _audit_lock:
                if now - _audit_last[0] < _audit_min_interval:
                    return
                _audit_last[0] = now
            return _orig_enqueue(text)

        _logger._enqueue = _paced_enqueue
        _logger._audit_pacing_patched = True

    _fm.install(_log)

    if not getattr(Analyzer, "_mtf_shadow_patched", False):
        _orig_analyze_mtf = Analyzer.analyze_mtf

        def _analyze_mtf_with_shadow(self, symbol, k15, k1h, k4h,
                                     min_score=60, fee_mult=2.0, vol_mult=1.0):
            result = _orig_analyze_mtf(
                self, symbol, k15, k1h, k4h,
                min_score=min_score, fee_mult=fee_mult, vol_mult=vol_mult,
            )
            try:
                before = _ms.snapshot().get("unique_states", 0)
                _ms.observe(
                    symbol, k15, k1h, k4h,
                    production_result=result,
                    min_score=min_score,
                    fee_mult=fee_mult,
                    vol_mult=vol_mult,
                )
                snap = _ms.snapshot()
                unique = snap.get("unique_states", 0)
                if unique != before and (unique == 1 or unique % 25 == 0):
                    _log.info(
                        "[MTF_SHADOW] unique=%s eligible=%s survivors=%s "
                        "nexus_approved=%s nexus_vetoed=%s execution_effect=NONE",
                        unique,
                        snap.get("eligible_4h_dir_1h_neutral", 0),
                        snap.get("shadow_pre_ai_survivors", 0),
                        snap.get("shadow_nexus_approved", 0),
                        snap.get("shadow_nexus_vetoed", 0),
                    )
            except Exception:
                pass
            return result

        Analyzer.analyze_mtf = _analyze_mtf_with_shadow
        Analyzer._mtf_shadow_patched = True

    if not getattr(TradingEngine, "_nexus_persistence_patched", False):
        _orig_validate = TradingEngine._nexus_validate
        _orig_status = getattr(TradingEngine, "get_status", None)

        async def _validate_with_history(self, sig):
            dec = await _orig_validate(self, sig)
            try:
                await _np.record_decision(sig, dec)
                asyncio.create_task(_np.evaluate_pending(self.client))
            except Exception:
                pass

            try:
                if getattr(dec, "execution_allowed", False) is not True:
                    asyncio.create_task(
                        _notifier.notify_nexus(dec.to_dict(), approved=False)
                    )
            except Exception:
                pass
            return dec

        TradingEngine._nexus_validate = _validate_with_history

        if _orig_status is not None:
            def _status_with_nexus_metrics(self, *args, **kwargs):
                out = _orig_status(self, *args, **kwargs)
                try:
                    if isinstance(out, dict):
                        out = dict(out)
                        out["nexus_persistent_metrics"] = _np.get_cached_metrics()
                        out["funnel_metrics"] = _fm.get_funnel_metrics()
                        out["mtf_shadow_metrics"] = _ms.snapshot()
                        out["nexus_dedupe_metrics"] = _nexus_decision_dedupe.snapshot()
                        if getattr(self, "paper_trade", False):
                            out["paper_wallet"] = {
                                "balance": round(float(getattr(self, "_paper_balance", self.risk.balance) or 0.0), 4),
                                "drawdown_pct": round(float(self.risk.drawdown) * 100.0, 2),
                                "isolated_from_exchange": True,
                            }
                except Exception:
                    pass
                return out
            TradingEngine.get_status = _status_with_nexus_metrics

        TradingEngine._nexus_persistence_patched = True
except Exception:
    pass
