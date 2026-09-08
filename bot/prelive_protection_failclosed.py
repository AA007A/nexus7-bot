"""Fail-closed protection hardening for the controlled live pilot.

This module does not grant execution permission. It tightens the existing
post-entry protection path so an unverifiable stop is treated as failure and
any pilot position still observed without a stop after an open attempt blocks
further entries.
"""
import asyncio


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

                confirmed_sl = float(pos.get("stopLoss", 0) or 0)
                if sl > 0 and confirmed_sl <= 0:
                    log.critical(
                        "[PROTECTION_FAIL_CLOSED] %s position is open without confirmed SL",
                        symbol,
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

            if live is not None and float(live.get("stopLoss", 0) or 0) <= 0:
                self._unprotected_symbols = set(
                    getattr(self, "_unprotected_symbols", set()) or set()
                )
                self._unprotected_symbols.add(sig.symbol)
                log.critical(
                    "[PROTECTION_POSTCONDITION] %s remains open without confirmed SL; new entries blocked",
                    sig.symbol,
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

    log.warning(
        "[PROTECTION_FAIL_CLOSED] installed: unverifiable SL is failure; pilot post-open unprotected positions block new entries"
    )
