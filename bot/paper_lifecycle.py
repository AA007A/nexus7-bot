"""PAPER-only position lifecycle.

The live engine reconciles positions against the exchange. In PAPER mode that is
incorrect because simulated positions deliberately do not exist at KuCoin.
This patch keeps PAPER positions internal, marks them to current market prices,
and simulates SL/TP exits without touching exchange state. LIVE is untouched.
"""
from __future__ import annotations

import time
from datetime import datetime


def install(log):
    import bot.engine as engine_module
    from bot.engine import TradingEngine, Trade
    from bot.config import cfg
    from bot.kucoin import TAKER_FEE
    from bot import database as db
    from bot.notifier import notify, close_msg

    if getattr(TradingEngine, "_paper_lifecycle_patched", False):
        return

    original_sync = TradingEngine._sync_positions

    async def _finish_paper_position(self, sym, pos, exit_px: float, reason: str):
        # Idempotency: another lifecycle rule may have closed it in this loop.
        if sym not in self.positions:
            return

        pos.update_pnl(exit_px)
        pnl_gross = pos.pnl
        fee_open = pos.qty * pos.entry * TAKER_FEE
        fee_close = pos.qty * exit_px * TAKER_FEE
        total_fee = fee_open + fee_close
        pnl_net = pnl_gross - total_fee

        trade = Trade(
            sym, pos.direction, pos.entry, exit_px,
            pos.qty, pnl_gross, pos.opened_at,
            fee_open=fee_open, fee_close=fee_close,
        )
        self.stats.add(trade)

        tid = self._trade_ids.pop(sym, 0)
        if tid:
            await db.save_trade_close(
                tid, exit_px, pnl_net, total_fee,
                (datetime.utcnow() - pos.opened_at).total_seconds() / 60,
                exit_reason=f"PAPER_{reason}",
            )

        del self.positions[sym]
        self._cooldown[sym] = time.time() + 1800
        await self._record_trade_result(sym, pnl_net)
        self.daily_tracker.add_pnl(
            pnl_net,
            symbol=sym,
            entry_type=getattr(pos, "entry_type", ""),
            regime=getattr(pos, "regime", ""),
            session=self._get_market_session(),
            rr_achieved=round(
                abs(pnl_net / max(abs(pos.entry - pos.sl), 0.0001) / pos.qty)
                if pos.sl and pos.qty else 0,
                2,
            ),
        )

        try:
            consecutive = await db.update_consecutive_losses(pnl_net)
            if consecutive >= 3:
                await db.save_risk_event(
                    "CONSECUTIVE_LOSSES",
                    f"{consecutive} perdas consecutivas",
                    pnl_net,
                )
        except Exception as exc:
            log.debug("PAPER close loss-counter: %s", exc)

        log.info(
            "📭 [PAPER] %s fechado por %s | exit=%.6f Bruto=$%+.4f Taxas=-$%.4f Líquido=$%+.4f",
            sym, reason, exit_px, pnl_gross, total_fee, pnl_net,
        )
        try:
            bal = await self.client.get_balance()
            await notify(await close_msg(
                sym, pos.direction, pnl_net, pos.pnl_pct(), exit_px,
                bal, bal * cfg.LEVERAGE,
            ))
        except Exception as exc:
            log.debug("PAPER close notify: %s", exc)

    async def paper_sync(self):
        if not self.paper_trade:
            return await original_sync(self)

        for sym, pos in list(self.positions.items()):
            try:
                # Prefer the WS ticker; REST is a fallback only when startup cache
                # has not received a price yet.
                tk = self.client.get_cached_ticker(sym) or {}
                price = float(tk.get("lastPrice", 0) or 0)
                if price <= 0:
                    fresh = await self.client.get_ticker(sym)
                    price = float((fresh or {}).get("lastPrice", 0) or 0)
                if price <= 0:
                    log.warning("[PAPER] %s sem preço atual — posição mantida", sym)
                    continue

                pos.update_pnl(price)

                sl = float(getattr(pos, "trailing_sl", 0) or getattr(pos, "sl", 0) or 0)
                tp = float(getattr(pos, "tp", 0) or 0)
                hit_sl = (
                    (pos.direction == "LONG" and sl > 0 and price <= sl)
                    or (pos.direction == "SHORT" and sl > 0 and price >= sl)
                )
                hit_tp = (
                    (pos.direction == "LONG" and tp > 0 and price >= tp)
                    or (pos.direction == "SHORT" and tp > 0 and price <= tp)
                )

                if hit_sl:
                    await _finish_paper_position(self, sym, pos, price, "SL")
                elif hit_tp:
                    await _finish_paper_position(self, sym, pos, price, "TP")
                else:
                    # Sparse heartbeat for E2E evidence; avoids flooding logs.
                    last = getattr(pos, "_paper_heartbeat", 0.0)
                    if time.time() - last >= 60:
                        pos._paper_heartbeat = time.time()
                        log.info(
                            "🧪 [PAPER_POS] %s %s entry=%.6f price=%.6f pnl=$%+.4f sl=%.6f tp=%.6f",
                            sym, pos.direction, pos.entry, price, pos.pnl, sl, tp,
                        )
            except Exception as exc:
                log.error("[PAPER] _sync_positions %s: %s", sym, exc)

    TradingEngine._sync_positions = paper_sync
    TradingEngine._paper_lifecycle_patched = True
    log.info("🧪 PAPER lifecycle: posições simuladas geridas internamente; LIVE sync inalterado")
