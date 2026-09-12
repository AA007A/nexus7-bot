"""Final PRE-LIVE daily-stop runtime hardening.

Keeps TradingEngine and DailyTracker synchronized so the daily stop uses the
configured limit and the current realized + unrealized daily PnL.

Simulation policy:
- LIVE keeps the daily stop fully blocking.
- PAPER keeps computing the same daily loss threshold, but treats that daily
  threshold as advisory so simulated entries can continue for validation.
- Weekly and monthly stops remain blocking in PAPER.

This module does not alter entry thresholds, leverage, exchange credentials, or
the explicit PAPER/LIVE authorization barrier.
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
        paper_mode = False
        configured_daily_stop = 0.0

        if tracker is not None:
            realized = float(self.stats.daily_pnl())
            unrealized = sum(
                float(getattr(p, "pnl", 0.0) or 0.0)
                for p in self.positions.values()
            )
            tracker.daily_pnl = realized + unrealized
            _sync_limits(self)
            configured_daily_stop = float(getattr(tracker, "daily_stop_loss", 0.0) or 0.0)

            # Reuse the exchange client's authoritative mode decision. This is
            # fail-closed: kucoin.PAPER_TRADE is False only when the explicit
            # LIVE authorization pair was accepted at import time.
            from bot import kucoin as _kucoin
            paper_mode = bool(getattr(_kucoin, "PAPER_TRADE", True))

            if paper_mode and configured_daily_stop > 0:
                # Disable only the DAILY threshold for the duration of the core
                # check. Weekly/monthly tracker limits remain active and can
                # still stop the simulation. The configured daily limit is
                # restored immediately afterwards for telemetry and dashboards.
                tracker.daily_stop_loss = 0.0

                if tracker.daily_pnl <= -configured_daily_stop:
                    from datetime import datetime, timezone

                    today = datetime.now(timezone.utc).date().isoformat()
                    if getattr(self, "_paper_daily_stop_advisory_day", None) != today:
                        self._paper_daily_stop_advisory_day = today
                        log.warning(
                            "[PAPER_DAILY_STOP_ADVISORY] pnl=$%.4f limit=-$%.4f "
                            "entries_blocked=false exchange_execution=false "
                            "weekly_monthly_stops_unchanged=true",
                            tracker.daily_pnl,
                            configured_daily_stop,
                        )

        try:
            result = orig_update_daily_pnl(self, *args, **kwargs)
        finally:
            if tracker is not None and paper_mode and configured_daily_stop > 0:
                tracker.daily_stop_loss = configured_daily_stop

        if tracker is not None:
            if paper_mode:
                # DAILY is advisory in PAPER only. Preserve higher-level stops.
                higher_level_stopped = bool(
                    getattr(tracker, "weekly_stopped", False)
                    or getattr(tracker, "monthly_stopped", False)
                )
                tracker.daily_stopped = False
                if not higher_level_stopped:
                    self.daily_stopped = False
            else:
                self.daily_stopped = bool(self.daily_stopped or tracker.daily_stopped)
        return result

    TradingEngine._connect = _connect_hardened
    TradingEngine._update_balance = _update_balance_hardened
    TradingEngine._check_daily_reset = _check_daily_reset_hardened
    TradingEngine._update_daily_pnl = _update_daily_pnl_hardened
    TradingEngine._daily_stop_runtime_hardened = True
    log.info(
        "[DAILY_STOP_RUNTIME] tracker synced with engine limits and current daily PnL; "
        "LIVE daily stop blocking=true; PAPER daily stop advisory=true"
    )
