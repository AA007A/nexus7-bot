"""Final PRE-LIVE daily-stop runtime hardening.

Keeps TradingEngine and DailyTracker synchronized so the daily stop uses the
configured limit and the current realized + unrealized daily PnL.

Controlled LIVE keeps the daily stop as a hard circuit breaker. In explicit
PAPER mode only, a triggered DAILY stop remains latched in DailyTracker for
telemetry but the engine-level daily entry-stop flag is cleared so simulated
validation can continue. Weekly/monthly stops are never bypassed here.
"""


def install(TradingEngine, log):
    if getattr(TradingEngine, "_daily_stop_runtime_hardened", False):
        return

    orig_connect = TradingEngine._connect
    orig_update_balance = TradingEngine._update_balance
    orig_check_daily_reset = TradingEngine._check_daily_reset
    orig_update_daily_pnl = TradingEngine._update_daily_pnl

    def _paper_mode() -> bool:
        # Single source of truth for whether place_order can have exchange
        # effects. KuCoin defaults fail-closed to PAPER unless LIVE is explicitly
        # acknowledged, so this cannot turn a LIVE process into advisory mode.
        from bot import kucoin
        return bool(getattr(kucoin, "PAPER_TRADE", True))

    def _daily_only_latched(tracker) -> bool:
        return bool(
            tracker is not None
            and getattr(tracker, "daily_stopped", False)
            and not getattr(tracker, "weekly_stopped", False)
            and not getattr(tracker, "monthly_stopped", False)
        )

    def _sync_limits(engine, *, sync_stop_state: bool = True):
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
        if sync_stop_state:
            tracker.daily_stopped = bool(getattr(engine, "daily_stopped", False))

    async def _connect_hardened(self, *args, **kwargs):
        result = await orig_connect(self, *args, **kwargs)
        _sync_limits(self)
        return result

    async def _update_balance_hardened(self, *args, **kwargs):
        result = await orig_update_balance(self, *args, **kwargs)
        _sync_limits(self)
        return result

    def _check_daily_reset_hardened(self, *args, **kwargs):
        # The core reset must see the synchronized configured stop before it
        # emits telemetry. After a real UTC-day reset it is safe to synchronize
        # the tracker stop state again because both sides should be cleared.
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

            # Once PAPER has latched a daily stop, do not copy the intentionally
            # cleared engine flag back into the tracker. Keeping the tracker
            # latched prevents check_limits() from re-emitting STOP every loop.
            preserve_paper_latch = _paper_mode() and _daily_only_latched(tracker)
            _sync_limits(self, sync_stop_state=not preserve_paper_latch)

        result = orig_update_daily_pnl(self, *args, **kwargs)

        if tracker is not None:
            self.daily_stopped = bool(self.daily_stopped or tracker.daily_stopped)

        if _paper_mode() and _daily_only_latched(tracker):
            self.daily_stopped = False
            # Keep tracker.daily_stopped=True as immutable evidence that the
            # daily circuit breaker was reached. Only the PAPER engine gate is
            # advisory; no LIVE execution permission is changed.
            marker = getattr(self, "_paper_daily_stop_advisory_marker", None)
            current_marker = (
                float(getattr(tracker, "daily_stop_loss", 0.0) or 0.0),
                int(getattr(tracker, "_last_reset_day", -1)),
            )
            if marker != current_marker:
                self._paper_daily_stop_advisory_marker = current_marker
                log.warning(
                    "[PAPER_DAILY_STOP_ADVISORY] daily_stop_triggered=true "
                    "tracker_latched=true entries_blocked=false "
                    "weekly_monthly_hard=true live_policy_unchanged=true "
                    "exchange_execution_possible=false"
                )

        return result

    TradingEngine._connect = _connect_hardened
    TradingEngine._update_balance = _update_balance_hardened
    TradingEngine._check_daily_reset = _check_daily_reset_hardened
    TradingEngine._update_daily_pnl = _update_daily_pnl_hardened
    TradingEngine._daily_stop_runtime_hardened = True
    log.info(
        "[DAILY_STOP_RUNTIME] tracker synced; live_daily_stop_hard=true "
        "paper_daily_stop_advisory=true weekly_monthly_hard=true"
    )
