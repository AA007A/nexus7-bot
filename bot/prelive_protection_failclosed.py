"""Fail-closed protection hardening for the controlled live pilot.

This module does not grant execution permission. It tightens the existing
post-entry protection path so an unverifiable stop is treated as failure and
any pilot position still observed without a stop after an open attempt blocks
further entries.

KuCoin Futures native TP/SL orders submitted through ``/api/v1/st-orders`` may
exist as separate conditional orders and therefore do not necessarily appear as
``stopLoss`` on the position payload. Protection checks must accept either an
inline stop OR a fully-covering conditional close/reduce-only stop confirmed by
the read-only conditional-stop verifier. This prevents a protected position
from being emergency-closed merely because ``position.stopLoss`` is zero.
"""
import asyncio

from bot.conditional_stop_protection import conditional_stop_confirmed


def install(TradingEngine, kucoin_mod, log):
    KuCoinClient = kucoin_mod.KuCoinClient

    if not getattr(KuCoinClient, "_strict_position_stops_patched", False):
        async def _strict_set_position_stops(self, symbol: str, sl: float = 0,
                                             tp: float = 0) -> bool:
            if kucoin_mod.PAPER_TRADE:
                log.info("[PAPER] set_position_stops: exchange mutation skipped")
                return False
            if not kucoin_mod.API_KEY:
                return False

            kc_sym = kucoin_mod.to_kucoin(symbol)
            body = {"symbol": kc_sym}
            if sl and sl > 0:
                body["stopLoss"] = self._round_price(sl, symbol)
            if tp and tp > 0:
                body["takeProfit"] = self._round_price(tp, symbol)
            if len(body) == 1:
                return False

            try:
                res = await self._post("/api/v1/position/trading-stop", body)
                if not res:
                    log.error("set_position_stops %s: exchange did not confirm request", symbol)
                    return False

                await asyncio.sleep(0.5)
                try:
                    positions = await self.get_positions()
                except Exception as exc:
                    log.error(
                        "[PROTECTION_FAIL_CLOSED] %s stop verification failed: %s",
                        symbol, type(exc).__name__,
                    )
                    return False

                pos = next((p for p in positions if p.get("symbol") == symbol), None)
                if not pos:
                    log.warning(
                        "[PROTECTION_FAIL_CLOSED] %s position absent during stop verification",
                        symbol,
                    )
                    return False

                # First accept the inline field. If KuCoin keeps protection as a
                # separate conditional order, verify it read-only instead of
                # falsely declaring the position naked.
                confirmed_sl = float(pos.get("stopLoss", 0) or 0)
                if sl > 0 and confirmed_sl <= 0:
                    protected, evidence = await conditional_stop_confirmed(self, pos)
                    if protected:
                        log.info(
                            "[PROTECTION_FAIL_CLOSED] %s SL confirmed via %s",
                            symbol, evidence,
                        )
                        return True
                    log.critical(
                        "[PROTECTION_FAIL_CLOSED] %s position is open without confirmed SL (%s)",
                        symbol, evidence,
                    )
                    return False

                if sl > 0 and confirmed_sl > 0:
                    if abs(confirmed_sl - sl) / sl > 0.01:
                        log.warning(
                            "[PROTECTION_FAIL_CLOSED] %s confirmed SL %.8f differs from requested %.8f",
                            symbol, confirmed_sl, sl,
                        )
                    log.info(
                        "[PROTECTION_FAIL_CLOSED] %s SL confirmed at %.8f",
                        symbol, confirmed_sl,
                    )
                return True
            except Exception as exc:
                log.error(
                    "[PROTECTION_FAIL_CLOSED] %s stop attachment failed: %s",
                    symbol, type(exc).__name__,
                )
                return False

        KuCoinClient.set_position_stops = _strict_set_position_stops
        KuCoinClient._strict_position_stops_patched = True

    if not getattr(TradingEngine, "_pilot_post_open_protection_patched", False):
        original_open = TradingEngine._open

        async def _open_with_pilot_protection_postcondition(self, sig, *args, **kwargs):
            result = await original_open(self, sig, *args, **kwargs)

            # This postcondition is exclusively for the controlled real pilot.
            # PAPER and validation-held SHADOW retain their existing behavior.
            if getattr(self, "paper_trade", False):
                return result
            pilot = getattr(self, "pilot", None)
            if pilot is None or not getattr(pilot, "enabled", False):
                return result
            if getattr(self, "_validation_safety_lock_active", False):
                return result

            try:
                positions = await self.client.get_positions()
                live = next(
                    (
                        p for p in positions
                        if p.get("symbol") == getattr(sig, "symbol", None)
                        and abs(float(p.get("size", 0) or 0)) > 0
                    ),
                    None,
                )
            except Exception as exc:
                live = None
                self._unprotected_symbols = set(
                    getattr(self, "_unprotected_symbols", set()) or set()
                )
                self._unprotected_symbols.add(getattr(sig, "symbol", "?"))
                log.critical(
                    "[PROTECTION_POSTCONDITION] %s reconciliation failed: %s; new entries blocked",
                    getattr(sig, "symbol", "?"), type(exc).__name__,
                )
                try:
                    from bot import durable_execution as durable
                    if getattr(self, "_durable_state_enforced", False):
                        durable._block(self, "orders")
                except Exception as block_exc:
                    log.error(
                        "[PROTECTION_POSTCONDITION] %s durable block failed: %s",
                        getattr(sig, "symbol", "?"), type(block_exc).__name__,
                    )
                return result

            if live is not None:
                protected, evidence = await conditional_stop_confirmed(self.client, live)
                if protected:
                    self._unprotected_symbols = set(
                        getattr(self, "_unprotected_symbols", set()) or set()
                    )
                    self._unprotected_symbols.discard(sig.symbol)
                    log.info(
                        "[PROTECTION_POSTCONDITION] %s protection confirmed via %s",
                        sig.symbol, evidence,
                    )
                else:
                    self._unprotected_symbols = set(
                        getattr(self, "_unprotected_symbols", set()) or set()
                    )
                    self._unprotected_symbols.add(sig.symbol)
                    log.critical(
                        "[PROTECTION_POSTCONDITION] %s remains open without confirmed SL (%s); new entries blocked",
                        sig.symbol, evidence,
                    )
                    try:
                        from bot import durable_execution as durable
                        if getattr(self, "_durable_state_enforced", False):
                            durable._block(self, "orders")
                    except Exception as block_exc:
                        log.error(
                            "[PROTECTION_POSTCONDITION] %s durable block failed: %s",
                            sig.symbol, type(block_exc).__name__,
                        )

            return result

        TradingEngine._open = _open_with_pilot_protection_postcondition
        TradingEngine._pilot_post_open_protection_patched = True

    # The canonical naked-position guard predates native /st-orders and checks
    # only position.stopLoss. Replace it with an equivalent fail-closed guard
    # that recognizes a confirmed conditional protective stop before attempting
    # legacy stop reattachment or emergency closure.
    if (hasattr(TradingEngine, "_guard_naked_positions")
            and not getattr(TradingEngine, "_conditional_naked_guard_patched", False)):
        async def _guard_naked_positions_with_conditional_protection(self):
            if getattr(self, "paper_trade", False):
                return
            try:
                positions = await self.client.get_positions()
            except Exception as exc:
                log.debug("_guard_naked_positions: %s", exc)
                return

            for p in positions:
                try:
                    sym = p.get("symbol", "")
                    size = float(p.get("size", 0) or 0)
                    if size <= 0:
                        continue

                    protected, evidence = await conditional_stop_confirmed(self.client, p)
                    if protected:
                        unprotected = set(
                            getattr(self, "_unprotected_symbols", set()) or set()
                        )
                        unprotected.discard(sym)
                        self._unprotected_symbols = unprotected
                        log.info(
                            "[PROTECTION_GUARD] %s protected via %s; emergency close not required",
                            sym, evidence,
                        )
                        continue

                    entry = float(p.get("entryPrice", 0) or 0)
                    side = p.get("side", "Buy")
                    if entry <= 0:
                        continue

                    log.critical(
                        "🚨 %s: POSIÇÃO SEM STOP LOSS confirmado na exchange "
                        "(size=%s entry=$%.4f evidence=%s) — reaplicando",
                        sym, size, entry, evidence,
                    )

                    if sym not in self.positions:
                        await self._reconcile_exchange_positions(only_symbol=sym)

                    pos_local = self.positions.get(sym)
                    if pos_local and getattr(pos_local, "sl", 0) > 0:
                        sl_target = pos_local.sl
                    else:
                        sl_target = entry * (0.985 if side == "Buy" else 1.015)

                    ok = await self.client.set_position_stops(sym, sl=sl_target)
                    if ok:
                        log.info("✓ %s: SL reaplicado @ $%.4f", sym, sl_target)
                        continue

                    # Keep the original safety behavior for a genuinely naked
                    # position: if protection cannot be confirmed, close it.
                    log.critical("🚨 %s: falha ao reaplicar SL — FECHANDO posição", sym)
                    res = await self.client.place_order(
                        symbol=sym,
                        side="Sell" if side == "Buy" else "Buy",
                        qty=size, sl=0, tp=0,
                        instruments=self.instruments,
                        reduce_only=True,
                    )
                    if res and res.get("orderId"):
                        self.positions.pop(sym, None)
                    else:
                        log.critical(
                            "[PROTECTION_GUARD] %s emergency close was not confirmed",
                            sym,
                        )
                except Exception as exc:
                    log.error(
                        "_guard_naked_positions %s: %s",
                        p.get("symbol", "?"), exc,
                    )

        TradingEngine._guard_naked_positions = _guard_naked_positions_with_conditional_protection
        TradingEngine._conditional_naked_guard_patched = True

    log.warning(
        "[PROTECTION_FAIL_CLOSED] installed: inline or independently confirmed conditional SL is accepted; genuinely unprotected pilot positions remain fail-closed"
    )
