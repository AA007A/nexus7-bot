"""Fail-closed engine readiness guard for KuCoin instrument metadata.

`main.py` deliberately starts the HTTP server before KuCoin instrument loading
finishes so Railway health checks do not deadlock deployment. Historically,
bootstrap logged instrument-load timeout/failure and still scheduled
`TradingEngine.run()`. That made process health look good even though contract
precision/minQty/multiplier metadata was unavailable.

This guard is installed from sitecustomize and prevents the engine scan loop
from starting unless the client exposes a non-empty instrument map. HTTP
liveness remains available; no exchange mutation or trading configuration is
changed.
"""
from __future__ import annotations


def _instrument_snapshot(client) -> dict:
    getter = getattr(client, "get_instruments", None)
    if callable(getter):
        try:
            value = getter()
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}
    value = getattr(client, "_instruments", None)
    return value if isinstance(value, dict) else {}


def install(log):
    from bot.engine import TradingEngine

    if getattr(TradingEngine, "_instrument_readiness_guard_patched", False):
        return

    original_run = TradingEngine.run
    original_status = getattr(TradingEngine, "get_status", None)

    async def _run_guarded(self, *args, **kwargs):
        instruments = _instrument_snapshot(getattr(self, "client", None))
        if not instruments:
            self.active = False
            self.connected = False
            self._instrument_readiness_blocked = True
            self._instrument_readiness_count = 0
            log.critical(
                "[INSTRUMENT_READINESS] engine blocked: KuCoin instrument metadata "
                "is empty; no scan/open loop will start"
            )
            return None

        self._instrument_readiness_blocked = False
        self._instrument_readiness_count = len(instruments)
        log.info(
            "[INSTRUMENT_READINESS] passed: instruments=%s; engine startup allowed",
            len(instruments),
        )
        return await original_run(self, *args, **kwargs)

    TradingEngine.run = _run_guarded

    if original_status is not None:
        def _status_with_readiness(self, *args, **kwargs):
            out = original_status(self, *args, **kwargs)
            if isinstance(out, dict):
                out = dict(out)
                instruments = _instrument_snapshot(getattr(self, "client", None))
                blocked = bool(getattr(self, "_instrument_readiness_blocked", False))
                out["instrument_readiness"] = {
                    "ready": bool(instruments) and not blocked,
                    "blocked": blocked,
                    "instrument_count": len(instruments),
                    "fail_closed": True,
                }
            return out
        TradingEngine.get_status = _status_with_readiness

    TradingEngine._instrument_readiness_guard_patched = True
    log.info(
        "[INSTRUMENT_READINESS] installed: empty contract metadata blocks engine startup"
    )
