"""Runtime-only observability hooks for NEXUS AI.

Loaded automatically by Python's site module. It wraps decision validation and
engine status without changing order submission, risk, sizing, or trading mode.
"""
import asyncio

try:
    from bot.engine import TradingEngine
    from bot import nexus_persistence as _np

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
                except Exception:
                    pass
                return out
            TradingEngine.get_status = _status_with_nexus_metrics

        TradingEngine._nexus_persistence_patched = True
except Exception:
    # Startup must remain fail-safe: telemetry hooks cannot block the app.
    pass
