"""Final PRE-LIVE daily-stop runtime hardening.

Keeps TradingEngine and DailyTracker synchronized so the daily stop uses the
configured limit and the current realized + unrealized daily PnL.

Controlled LIVE keeps the daily stop as a hard circuit breaker. PAPER and
read-only SHADOW keep recording the same stop event but clear only the daily
entry-stop flag afterward so validation can continue without exchange effects.
"""


def install(TradingEngine, log):
    if getattr(TradingEngine, "_daily_stop_runtime_hardened", False):
        return

    orig_connect = TradingEngine._connect
    orig_update_balance = TradingEngine._update_balance
    orig_check_daily_reset = TradingEngine._check_daily_reset
    orig_update_daily_pnl = TradingEngine._update_daily_pnl

    def _sync_limits(engine):
        from bot.config import cfg

        tracker = getattr(engine, "daily_tracker", None)
        if tracker is None:
            return

        balance = float(getattr(getattr(engine, "risk", None), "balance", 0.0) or 0.0)
        target = float(getattr(engine, "daily_target", 0.0) or 0.0)
        stop = float(getattr(engine, "daily_stop_loss", 0.0) or 0.0)

        if balance > 0:
            if float(getattr(cfg, "DAILY_TARGET", 0.0) or 0.0) > 0:
                target = float(cfg.DAILY_TARGET)
            else:
                target = round(balance * float(cfg.DAILY_TARGET_PCT), 2)

            if float(getattr(cfg, "DAILY_STOP_LOSS", 0.0) or 0.0) > 0:
                stop = float(cfg.DAILY_STOP_LOSS)
            else:
                stop = round(balance * float(cfg.DAILY_STOP_LOSS_PCT), 2)

            engine.daily_target = target
            engine.daily_stop_loss = stop

        tracker.daily_target = target
        tracker.daily_stop_loss = stop
        tracker.daily_stopped = bool(getattr(engine, "daily_stopped", False))

    def _paper_or_shadow(engine) -> bool:
        return bool(
            getattr(engine, "paper_trade", False)
            or getattr(engine, "_validation_safety_lock_active", False)
        )

    async def _connect_hardened(self, *args, **kwargs):
        result = await orig_connect(self, *args, **kwargs)
        _sync_limits(self)
        return result

    async def _update_balance_hardened(self, *args, **kwargs):
        result = await orig_update_balance(self, *args, **kwargs)
        _sync_limits(self)
        return result

    def _check_daily_reset_hardened(self, *args, **kwargs):
        # The legacy reset logs the current stop before the previous hardening
        # wrapper had a chance to synchronize it. Sync first so operator-facing
        # reset telemetry reflects the same configured limit that enforcement
        # uses, then sync again in case the core reset changes daily state.
        _sync_limits(self)
        result = orig_check_daily_reset(self, *args, **kwargs)
        _sync_limits(self)
        return result

    def _update_daily_pnl_hardened(self, *args, **kwargs):
        tracker = getattr(self, "daily_tracker", None)
        if tracker is not None:
            realized = float(self.stats.daily_pnl())
            unrealized = sum(
                float(getattr(p, "pnl", 0.0) or 0.0)
                for p in self.positions.values()
            )
            tracker.daily_pnl = realized + unrealized
            _sync_limits(self)

        result = orig_update_daily_pnl(self, *args, **kwargs)

        if tracker is not None:
            self.daily_stopped = bool(self.daily_stopped or tracker.daily_stopped)

        if _paper_or_shadow(self) and bool(getattr(self, "daily_stopped", False)):
            self.daily_stopped = False
            if tracker is not None:
                tracker.daily_stopped = False
            log.warning(
                "[PAPER_SHADOW_DAILY_STOP_ADVISORY] mode=%s daily_stop_triggered=true "
                "entries_blocked=false live_policy_unchanged=true execution_effect=NONE",
                "PAPER" if getattr(self, "paper_trade", False) else "SHADOW",
            )

        return result

    TradingEngine._connect = _connect_hardened
    TradingEngine._update_balance = _update_balance_hardened
    TradingEngine._check_daily_reset = _check_daily_reset_hardened
    TradingEngine._update_daily_pnl = _update_daily_pnl_hardened
    TradingEngine._daily_stop_runtime_hardened = True
    log.info(
        "[DAILY_STOP_RUNTIME] tracker synced; live_daily_stop_hard=true "
        "paper_shadow_daily_stop_advisory=true"
    )
