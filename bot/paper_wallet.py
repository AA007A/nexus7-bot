"""PAPER-only virtual wallet isolation.

A PAPER session must not treat deposits, withdrawals, manual trades, margin
reservation, or any other KuCoin account balance movement as bot PnL. This
module snapshots the observed startup balance once, then keeps an internal
virtual wallet driven only by simulated PAPER trade results.

LIVE behavior is untouched. Trading thresholds, leverage, risk parameters and
all production decision gates are unchanged.
"""
from __future__ import annotations


def install(log):
    from bot.engine import TradingEngine
    from bot.kucoin import TAKER_FEE

    if getattr(TradingEngine, "_paper_wallet_patched", False):
        return

    original_connect = TradingEngine._connect
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

    async def _connect_with_paper_wallet(self):
        await original_connect(self)
        if not getattr(self, "paper_trade", False):
            return
        if not getattr(self, "connected", False):
            return

        # Snapshot exactly once per process. From this point on, external
        # KuCoin balance changes cannot create PAPER drawdown/profit.
        if not hasattr(self, "_paper_balance"):
            initial = float(getattr(self.risk, "balance", 0.0) or 0.0)
            self._paper_balance = initial
            self.risk.peak_balance = initial
            self.risk.balance = initial
            self.risk.drawdown = 0.0
            self.risk.balance_confirmed = initial > 0
            log.info(
                "🧪 PAPER wallet isolada: saldo virtual inicial=$%.4f; "
                "mudanças posteriores na conta KuCoin não alteram PnL/drawdown PAPER",
                initial,
            )

    async def _update_balance_paper_safe(self):
        if not getattr(self, "paper_trade", False):
            return await original_update_balance(self)

        bal = float(getattr(self, "_paper_balance", self.risk.balance) or 0.0)
        self.risk.update(bal)

        # Preserve the engine's dynamic daily limits, but derive them from the
        # virtual PAPER wallet instead of the external account balance.
        from bot.config import cfg
        if bal > 0:
            self.daily_target = round(bal * cfg.DAILY_TARGET_PCT, 2)
            self.daily_stop_loss = round(bal * cfg.DAILY_STOP_LOSS_PCT, 2)

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

            # Match engine._manage_partial_tp accounting exactly.
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

    TradingEngine._connect = _connect_with_paper_wallet
    TradingEngine._update_balance = _update_balance_paper_safe
    TradingEngine._refresh_entry_balance = _refresh_entry_balance_paper_safe
    TradingEngine._manage_partial_tp = _manage_partial_tp_with_wallet
    TradingEngine._paper_apply_virtual_balance = _apply_virtual_balance
    TradingEngine._paper_wallet_patched = True

    log.info(
        "🧪 PAPER wallet isolation installed; LIVE balance path unchanged"
    )
