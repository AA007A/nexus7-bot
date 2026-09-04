"""PAPER-only position lifecycle.

The live engine reconciles positions against the exchange. In PAPER mode that is
incorrect because simulated positions deliberately do not exist at KuCoin.
This patch keeps PAPER positions internal, marks them to current market prices,
simulates SL/TP exits, and keeps the live integrity/exchange path untouched.
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
    from bot.integrity import IntegrityGuard, IntegrityState, Severity

    if getattr(TradingEngine, "_paper_lifecycle_patched", False):
        return

    original_sync = TradingEngine._sync_positions
    original_init = TradingEngine.__init__
    original_assess = IntegrityGuard.assess

    def init_with_paper_stop_sim(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        if not self.paper_trade:
            return

        client = self.client
        if getattr(client, "_paper_stop_sim_patched", False):
            return

        client._real_set_sl = getattr(client, "set_sl", None)
        client._real_set_position_stops = getattr(client, "set_position_stops", None)

        async def _paper_set_sl(symbol: str, sl: float):
            log.info("[PAPER] set_sl simulated OK: %s -> %.6f", symbol, sl)
            return True

        async def _paper_set_position_stops(symbol: str, sl: float = 0, tp: float = 0):
            log.info(
                "[PAPER] set_position_stops simulated OK: %s SL=%.6f TP=%.6f",
                symbol, sl, tp,
            )
            return True

        client.set_sl = _paper_set_sl
        client.set_position_stops = _paper_set_position_stops
        client._paper_stop_sim_patched = True
        log.info("🧪 PAPER stops: acknowledgements simulados; nenhuma mutação na exchange")

    async def assess_with_paper_semantics(self, client, engine):
        if not getattr(engine, "paper_trade", False):
            return await original_assess(self, client, engine)

        original_log_state = self._log_state
        self._log_state = lambda: None
        try:
            state = await original_assess(self, client, engine)
        finally:
            self._log_state = original_log_state

        kept = []
        removed = []
        for issue in state.issues:
            if issue.code == "STATE_DIVERGENCE" and "INEXISTENTE na exchange" in issue.detail:
                removed.append(issue)
            else:
                kept.append(issue)

        if removed:
            if any(i.severity == Severity.BLOCKED for i in kept):
                sev = Severity.BLOCKED
            elif any(i.severity == Severity.DEGRADED for i in kept):
                sev = Severity.DEGRADED
            else:
                sev = Severity.OK

            self.state = IntegrityState(
                severity=sev,
                issues=kept,
                checked_at=state.checked_at,
                exchange_known=state.exchange_known,
            )
            log.debug(
                "[PAPER] integrity: ignorada divergência esperada de %d posição(ões) simulada(s)",
                len(removed),
            )
        else:
            self.state = state

        self._log_state()
        return self.state

    async def _finish_paper_position(self, sym, pos, exit_px: float, reason: str):
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

        # Settle the simulated result into the isolated PAPER wallet. This is
        # intentionally absent in LIVE and cannot mutate the KuCoin balance.
        try:
            apply_wallet = getattr(self, "_paper_apply_virtual_balance", None)
            if callable(apply_wallet):
                current = float(getattr(self, "_paper_balance", self.risk.balance) or 0.0)
                apply_wallet(current + pnl_net, f"close:{sym}:{reason}:{pnl_net:+.4f}")
        except Exception as exc:
            log.error("[PAPER_WALLET] close settlement failed %s: %s", sym, exc)

        log.info(
            "📭 [PAPER] %s fechado por %s | exit=%.6f Bruto=$%+.4f Taxas=-$%.4f Líquido=$%+.4f",
            sym, reason, exit_px, pnl_gross, total_fee, pnl_net,
        )
        try:
            bal = float(getattr(self, "_paper_balance", self.risk.balance) or 0.0)
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
                    last = getattr(pos, "_paper_heartbeat", 0.0)
                    if time.time() - last >= 60:
                        pos._paper_heartbeat = time.time()
                        log.info(
                            "🧪 [PAPER_POS] %s %s entry=%.6f price=%.6f pnl=$%+.4f sl=%.6f tp=%.6f",
                            sym, pos.direction, pos.entry, price, pos.pnl, sl, tp,
                        )
            except Exception as exc:
                log.error("[PAPER] _sync_positions %s: %s", sym, exc)

    TradingEngine.__init__ = init_with_paper_stop_sim
    TradingEngine._sync_positions = paper_sync
    IntegrityGuard.assess = assess_with_paper_semantics
    TradingEngine._paper_lifecycle_patched = True
    log.info(
        "🧪 PAPER lifecycle: posições/stops/integridade simulados internamente; LIVE inalterado"
    )
