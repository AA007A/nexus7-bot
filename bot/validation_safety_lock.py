"""Validation safety lock with read-only SHADOW LIVE support.

When PAPER_TRADE is disabled during validation, the runtime may consume live
market/account data and exercise the decision pipeline, but it must fail closed
before any exchange mutation. PAPER mode is unchanged.
"""

SHADOW_MIN_ENTRY_SCORE = 55


def install(log):
    from bot.engine import TradingEngine

    if getattr(TradingEngine, "_validation_safety_lock_patched", False):
        return

    original_connect = TradingEngine._connect
    original_open = TradingEngine._open
    original_sync = TradingEngine._sync_positions
    original_update_balance = TradingEngine._update_balance
    original_effective_score = TradingEngine._effective_score
    original_reconcile = getattr(TradingEngine, "_reconcile_exchange_positions", None)
    original_guard = getattr(TradingEngine, "_guard_naked_positions", None)

    async def _connect_locked(self, *args, **kwargs):
        if getattr(self, "paper_trade", False):
            return await original_connect(self, *args, **kwargs)
        self._validation_safety_lock_active = True
        from bot import shadow_live
        from bot import shadow_integrity_isolation
        shadow_integrity_isolation.install_for_engine(self, log)
        log.warning(
            "[VALIDATION_LOCK] LIVE mutation blocked; starting read-only SHADOW LIVE pipeline"
        )
        return await shadow_live.connect_readonly(self)

    async def _open_locked(self, sig, *args, **kwargs):
        if getattr(self, "paper_trade", False):
            return await original_open(self, sig, *args, **kwargs)
        self._validation_safety_lock_active = True
        from bot import shadow_live
        return await shadow_live.evaluate_candidate(self, sig)

    async def _sync_locked(self, *args, **kwargs):
        if getattr(self, "paper_trade", False):
            return await original_sync(self, *args, **kwargs)
        return None

    async def _update_balance_locked(self, *args, **kwargs):
        if getattr(self, "paper_trade", False):
            return await original_update_balance(self, *args, **kwargs)
        if not getattr(self, "_validation_safety_lock_active", False):
            # Independent fail-closed guard: non-PAPER execution is never
            # allowed to silently inherit SHADOW-only balance semantics.
            log.critical(
                "[VALIDATION_LOCK] non-PAPER periodic balance refresh blocked: "
                "validation lock is not active"
            )
            return None
        from bot import shadow_balance_semantics
        state = await shadow_balance_semantics.refresh_shadow_risk(self)
        log.info(
            "[SHADOW_PERIODIC_BALANCE] equity=%.4f available=%.4f "
            "drawdown=%.2f%% execution_effect=NONE",
            float(state["equity"]),
            float(state["available"]),
            self.risk.drawdown * 100.0,
        )
        return state

    def _effective_score_locked(self):
        if getattr(self, "paper_trade", False):
            return original_effective_score(self)
        if getattr(self, "_validation_safety_lock_active", False):
            return SHADOW_MIN_ENTRY_SCORE
        return original_effective_score(self)

    TradingEngine._connect = _connect_locked
    TradingEngine._open = _open_locked
    TradingEngine._sync_positions = _sync_locked
    TradingEngine._update_balance = _update_balance_locked
    TradingEngine._effective_score = _effective_score_locked

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

    # Observability-only wrapper around the NEXUS call. It records latency and
    # guarantees a terminal Telegram result while leaving approval criteria and
    # exchange execution untouched.
    from bot import nexus_latency_telegram
    from bot import notifier
    nexus_latency_telegram.install(TradingEngine, notifier, log)

    TradingEngine._validation_safety_lock_patched = True
    log.warning(
        "[VALIDATION_LOCK] installed: PAPER unaffected; LIVE mutations blocked; "
        "SHADOW LIVE read-only analysis enabled; shadow_min_entry_score=%s",
        SHADOW_MIN_ENTRY_SCORE,
    )
