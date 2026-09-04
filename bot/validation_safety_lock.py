"""Temporary validation safety lock.

While NEXUS-7 is undergoing PAPER validation, any runtime that starts with
PAPER_TRADE disabled must fail closed before exchange-facing engine activity.
This does not change Railway variables and does not affect PAPER mode.

Remove this module from sitecustomize only after validation is complete and a
separate, explicit live-readiness decision is made.
"""


def install(log):
    from bot.engine import TradingEngine

    if getattr(TradingEngine, "_validation_safety_lock_patched", False):
        return

    original_connect = TradingEngine._connect
    original_open = TradingEngine._open
    original_sync = TradingEngine._sync_positions
    original_reconcile = getattr(TradingEngine, "_reconcile_exchange_positions", None)
    original_guard = getattr(TradingEngine, "_guard_naked_positions", None)

    async def _connect_locked(self, *args, **kwargs):
        if getattr(self, "paper_trade", False):
            return await original_connect(self, *args, **kwargs)
        self.active = False
        self.connected = False
        log.critical(
            "[VALIDATION_LOCK] LIVE runtime blocked: PAPER validation is not complete; "
            "no exchange-facing engine connection will be started"
        )
        return False

    async def _open_locked(self, *args, **kwargs):
        if getattr(self, "paper_trade", False):
            return await original_open(self, *args, **kwargs)
        log.critical("[VALIDATION_LOCK] real order opening blocked")
        return None

    async def _sync_locked(self, *args, **kwargs):
        if getattr(self, "paper_trade", False):
            return await original_sync(self, *args, **kwargs)
        return None

    TradingEngine._connect = _connect_locked
    TradingEngine._open = _open_locked
    TradingEngine._sync_positions = _sync_locked

    if original_reconcile is not None:
        async def _reconcile_locked(self, *args, **kwargs):
            if getattr(self, "paper_trade", False):
                return await original_reconcile(self, *args, **kwargs)
            return []
        TradingEngine._reconcile_exchange_positions = _reconcile_locked

    if original_guard is not None:
        async def _guard_locked(self, *args, **kwargs):
            if getattr(self, "paper_trade", False):
                return await original_guard(self, *args, **kwargs)
            return None
        TradingEngine._guard_naked_positions = _guard_locked

    TradingEngine._validation_safety_lock_patched = True
    log.warning(
        "[VALIDATION_LOCK] installed: PAPER unaffected; LIVE engine activity blocked"
    )
