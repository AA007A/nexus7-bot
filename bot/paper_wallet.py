"""PAPER-only virtual wallet isolation.

PAPER capital must not depend on deposits, withdrawals, manual trades, margin
reservation, or any other KuCoin account-balance movement. The simulator uses
explicit virtual capital when configured and then changes that capital only
through simulated PAPER results.

LIVE behavior, trading thresholds, leverage, risk parameters and production
decision gates are unchanged.
"""
from __future__ import annotations

import os


def install(log):
    from bot.engine import TradingEngine
    from bot.kucoin import TAKER_FEE

    if getattr(TradingEngine, "_paper_wallet_patched", False):
        return

    original_connect = TradingEngine._connect
    original_filter_viable = TradingEngine._filter_viable_symbols
    original_update_balance = TradingEngine._update_balance
    original_refresh_entry_balance = TradingEngine._refresh_entry_balance
    original_manage_partial_tp = TradingEngine._manage_partial_tp

    def _apply_virtual_balance(self, new_balance: float, reason: str):
        new_balance = max(0.0, float(new_balance))
        self._paper_balance = new_balance
        self.risk.update(new_balance)
        log.info(
            "[PAPER_WALLET] balance=$%.4f drawdown=%.2f%% reason=%s",
            new_balance,
            self.risk.drawdown * 100.0,
            reason,
        )

    def _virtual_initial_balance(observed_balance: float) -> tuple[float, str]:
        """Choose PAPER capital without depending on mutable exchange balance."""
        from bot.config import cfg

        raw = os.environ.get("PAPER_INITIAL_BALANCE", "").strip()
        if raw:
            try:
                configured = float(raw)
                if configured > 0:
                    return configured, "PAPER_INITIAL_BALANCE"
            except (TypeError, ValueError):
                log.warning(
                    "[PAPER_WALLET] PAPER_INITIAL_BALANCE inválido; usando fallback seguro"
                )

        configured = float(getattr(cfg, "INITIAL_CAP", 0.0) or 0.0)
        if configured > 0:
            return configured, "INITIAL_CAP"

        observed = max(0.0, float(observed_balance or 0.0))
        if observed > 0:
            return observed, "startup_exchange_snapshot"

        return 0.0, "unconfirmed_zero"

    def _sync_daily_limits(self, balance: float):
        """Keep engine and DailyTracker limits on the same PAPER capital."""
        from bot.config import cfg

        if balance <= 0:
            return
        if cfg.DAILY_TARGET <= 0:
            self.daily_target = round(balance * cfg.DAILY_TARGET_PCT, 2)
        if cfg.DAILY_STOP_LOSS <= 0:
            self.daily_stop_loss = round(balance * cfg.DAILY_STOP_LOSS_PCT, 2)
        try:
            self.daily_tracker.recalc_limits(balance)
            self.daily_tracker.daily_target = self.daily_target
            self.daily_tracker.daily_stop_loss = self.daily_stop_loss
        except Exception as exc:
            log.warning("[PAPER_WALLET] daily-limit sync failed: %s", exc)

    async def _filter_viable_symbols_paper_safe(self):
        """Use virtual buying power during PAPER startup viability filtering."""
        if not getattr(self, "paper_trade", False):
            return await original_filter_viable(self)

        current = float(getattr(self.risk, "balance", 0.0) or 0.0)
        virtual, source = _virtual_initial_balance(current)
        if virtual > 0 and current <= 0:
            # original _connect() reads the authenticated exchange balance before
            # viability filtering. In PAPER that balance may legitimately be 0.
            # Seed RiskManager with simulator capital before the filter so a
            # harmless exchange-balance state cannot produce ZERO_VIABLE_SYMBOLS.
            self.risk.update(virtual)
            self.risk.balance_confirmed = True
            log.info(
                "[PAPER_WALLET] viability uses virtual capital=$%.4f source=%s",
                virtual,
                source,
            )
        return await original_filter_viable(self)

    async def _connect_with_paper_wallet(self):
        await original_connect(self)
        if not getattr(self, "paper_trade", False):
            return
        if not getattr(self, "connected", False):
            return

        if not hasattr(self, "_paper_balance"):
            observed = float(getattr(self.risk, "balance", 0.0) or 0.0)
            initial, source = _virtual_initial_balance(observed)
            self._paper_balance = initial
            self.risk.peak_balance = initial
            self.risk.balance = initial
            self.risk.drawdown = 0.0
            self.risk.balance_confirmed = initial > 0
            _sync_daily_limits(self, initial)
            log.info(
                "🧪 PAPER wallet isolada: saldo virtual inicial=$%.4f source=%s; "
                "mudanças na conta KuCoin não alteram PnL/drawdown PAPER",
                initial,
                source,
            )

    async def _update_balance_paper_safe(self):
        if not getattr(self, "paper_trade", False):
            return await original_update_balance(self)

        bal = float(getattr(self, "_paper_balance", self.risk.balance) or 0.0)
        self.risk.update(bal)
        _sync_daily_limits(self, bal)

        from bot.config import cfg
        if self.risk.drawdown >= cfg.MAX_DRAWDOWN:
            if not getattr(self, "_dd_alerted", False):
                self._dd_alerted = True
                self.active = False
                log.warning(
                    "🚨 [PAPER] Drawdown virtual %.1f%% ≥ %.0f%% → pausando entradas",
                    self.risk.drawdown * 100.0,
                    cfg.MAX_DRAWDOWN * 100.0,
                )
        else:
            self._dd_alerted = False

    async def _refresh_entry_balance_paper_safe(self):
        if not getattr(self, "paper_trade", False):
            return await original_refresh_entry_balance(self)

        bal = float(getattr(self, "_paper_balance", self.risk.balance) or 0.0)
        try:
            self.risk.update(bal)
        except Exception:
            self.risk.balance_confirmed = False
            return False
        if bal <= 0:
            log.warning("[PAPER_WALLET] entry blocked: virtual balance <= 0")
            return False
        return True

    async def _manage_partial_tp_with_wallet(self):
        if not getattr(self, "paper_trade", False):
            return await original_manage_partial_tp(self)

        before = {}
        for sym, pos in self.positions.items():
            before[sym] = {
                "qty": float(getattr(pos, "qty", 0.0) or 0.0),
                "tp1_hit": bool(getattr(pos, "tp1_hit", False)),
                "entry": float(getattr(pos, "entry", 0.0) or 0.0),
                "sl": float(getattr(pos, "sl", 0.0) or 0.0),
            }

        result = await original_manage_partial_tp(self)

        for sym, old in before.items():
            pos = self.positions.get(sym)
            if pos is None:
                continue
            if old["tp1_hit"] or not getattr(pos, "tp1_hit", False):
                continue

            qty_after = float(getattr(pos, "qty", 0.0) or 0.0)
            partial_qty = max(0.0, old["qty"] - qty_after)
            if partial_qty <= 0:
                continue

            risk_dist = abs(old["entry"] - old["sl"])
            cur = float(getattr(pos, "current_price", old["entry"]) or old["entry"])
            pnl_partial = risk_dist * partial_qty
            fee_p = partial_qty * cur * TAKER_FEE * 2
            pnl_net = pnl_partial - fee_p
            _apply_virtual_balance(
                self,
                float(getattr(self, "_paper_balance", self.risk.balance)) + pnl_net,
                f"partial_tp:{sym}:{pnl_net:+.4f}",
            )

        return result

    TradingEngine._filter_viable_symbols = _filter_viable_symbols_paper_safe
    TradingEngine._connect = _connect_with_paper_wallet
    TradingEngine._update_balance = _update_balance_paper_safe
    TradingEngine._refresh_entry_balance = _refresh_entry_balance_paper_safe
    TradingEngine._manage_partial_tp = _manage_partial_tp_with_wallet
    TradingEngine._paper_apply_virtual_balance = _apply_virtual_balance
    TradingEngine._paper_wallet_patched = True

    log.info("🧪 PAPER wallet isolation installed; LIVE balance path unchanged")
