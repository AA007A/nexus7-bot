"""Runtime-only observability and infrastructure hardening for NEXUS AI.

Loaded automatically by Python's site module. It wraps decision validation,
engine status, and the Analyzer for shadow-only A/B measurement without
changing order submission, risk, sizing, strategy return values, or trading mode.
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
    from bot.logger import log as _log

    # Infrastructure-only hardening. Neither patch changes signal/risk/order
    # criteria or trading mode.
    _rh.install_database_schema_fix(_log)
    _rh.install_telegram_fix(_log)

    # Pace the independent NEXUS audit mirror. A burst of many symbols used to
    # enqueue messages back-to-back and trigger Telegram flood control. Audit
    # telemetry is lossy by design; dropping burst duplicates is preferable to
    # blocking the Telegram channel.
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

    # Passive log observer: counts where the pre-NEXUS funnel is stopping.
    _fm.install(_log)

    # Shadow A/B observer for the strict 4H+1H alignment gate.
    # IMPORTANT: it always returns the production Analyzer result unchanged.
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
                # Sparse, machine-readable progress marker for Railway audits.
                # It is emitted only when a new closed-candle state is observed,
                # at the first state and then every 25 unique states.
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
                # Shadow telemetry can never affect the strategy result.
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
                # Evaluate previously frozen decisions with future candles only.
                asyncio.create_task(_np.evaluate_pending(self.client))
            except Exception:
                # Observability must never affect the execution gate.
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
                except Exception:
                    pass
                return out
            TradingEngine.get_status = _status_with_nexus_metrics

        TradingEngine._nexus_persistence_patched = True
except Exception:
    # Startup must remain fail-safe: telemetry/hooks cannot block the app.
    pass
