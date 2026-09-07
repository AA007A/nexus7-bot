"""Fix the stagnation exit to use elapsed trade time, not cache depth.

The production engine keeps up to 100 historical 15m candles in cache. Using
``len(k15)`` as ``bars_open`` therefore makes a brand-new position look ~25h
old and can trigger the 4h stagnation exit immediately. This runtime hardening
preserves the existing exit rules while computing elapsed 15m bars from
``Position.opened_at``.
"""
from datetime import datetime, timezone
import time

INTERVAL_SECONDS = 15 * 60
STAGNATION_BARS = 16
STAGNATION_MULT = 0.5


def bars_since_open(opened_at, *, now_ts=None):
    """Return completed 15m intervals since ``opened_at``; never negative."""
    if isinstance(opened_at, datetime):
        dt = opened_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        opened_ts = dt.timestamp()
    else:
        opened_ts = float(opened_at)
    if now_ts is None:
        now_ts = time.time()
    return max(0, int((float(now_ts) - opened_ts) // INTERVAL_SECONDS))


def install(TradingEngine, log):
    """Install the corrected position-management method once."""
    if getattr(TradingEngine, "_stagnation_time_patched", False):
        return

    async def _check_stagnation_and_invalidation(self):
        from bot.indicators import atr as calc_atr
        from bot.strategy import detect_regime

        for sym, pos in list(self.positions.items()):
            try:
                k15 = self.client.get_cached_klines(sym, "15", limit=100) or []
                if len(k15) < 20:
                    continue

                closes = [float(k["c"]) for k in k15[:-1]]
                highs = [float(k["h"]) for k in k15[:-1]]
                lows = [float(k["l"]) for k in k15[:-1]]
                cur = pos.current_price or closes[-1]

                atr_val = float(calc_atr(highs, lows, closes, 14)[-1])
                if atr_val <= 0:
                    continue

                # Preserve the existing rule: only after 4h / 16 completed 15m bars.
                movement = abs(cur - pos.entry)
                bars_open = bars_since_open(pos.opened_at)
                if (bars_open >= STAGNATION_BARS
                        and movement < atr_val * STAGNATION_MULT):
                    log.info(
                        f"⏱️  [{sym}] Saída por TEMPO: {bars_open} candles aberto, "
                        f"movimento={movement:.4f} < {atr_val*STAGNATION_MULT:.4f} "
                        f"(0.5×ATR) → fechando para evitar funding acumulado"
                    )
                    close_side = "Sell" if pos.direction == "LONG" else "Buy"
                    await self.client.place_order(
                        symbol=sym, side=close_side,
                        qty=pos.qty, sl=0, tp=0,
                        instruments=self.instruments,
                        reduce_only=True,
                    )
                    continue

                # Preserve invalidation exit independently of position age.
                if len(closes) >= 10:
                    recent_c = closes[-10:]
                    recent_h = highs[-10:]
                    recent_l = lows[-10:]
                    choch_bear = (
                        recent_h[-1] < recent_h[-3] and
                        recent_l[-1] < recent_l[-3] and
                        recent_c[-1] < recent_c[-3]
                    )
                    choch_bull = (
                        recent_l[-1] > recent_l[-3] and
                        recent_h[-1] > recent_h[-3] and
                        recent_c[-1] > recent_c[-3]
                    )
                    invalidated = (
                        (pos.direction == "LONG" and choch_bear) or
                        (pos.direction == "SHORT" and choch_bull)
                    )
                    if invalidated and not pos.tp1_hit:
                        log.info(
                            f"❌ [{sym}] Saída por INVALIDAÇÃO: CHoCH oposto detectado "
                            f"após entrada {pos.direction} → fechando antes do SL"
                        )
                        close_side = "Sell" if pos.direction == "LONG" else "Buy"
                        await self.client.place_order(
                            symbol=sym, side=close_side,
                            qty=pos.qty, sl=0, tp=0,
                            instruments=self.instruments,
                            reduce_only=True,
                        )
                        continue

                # Preserve regime-change exit independently of position age.
                k4h = self.client.get_cached_klines(sym, "240", limit=120) or []
                if len(k4h) >= 20:
                    c4h = [float(k["c"]) for k in k4h[:-1]]
                    h4h = [float(k["h"]) for k in k4h[:-1]]
                    l4h = [float(k["l"]) for k in k4h[:-1]]
                    atr4h = float(calc_atr(h4h, l4h, c4h, 14)[-1])
                    regime_now = detect_regime(c4h, h4h, l4h, atr4h)
                    if regime_now in ("RANGING", "COMPRESSED", "CHOPPY") and not pos.tp1_hit:
                        log.info(
                            f"🔄 [{sym}] Saída por REGIME: mercado mudou para "
                            f"{regime_now} → setup trend-follow inválido, fechando"
                        )
                        close_side = "Sell" if pos.direction == "LONG" else "Buy"
                        await self.client.place_order(
                            symbol=sym, side=close_side,
                            qty=pos.qty, sl=0, tp=0,
                            instruments=self.instruments,
                            reduce_only=True,
                        )
            except Exception as exc:
                log.error(f"_check_stagnation_and_invalidation {sym}: {exc}")

    TradingEngine._check_stagnation_and_invalidation = _check_stagnation_and_invalidation
    TradingEngine._stagnation_time_patched = True
    log.info("[STAGNATION_TIME] installed: bars_open uses elapsed trade time")
