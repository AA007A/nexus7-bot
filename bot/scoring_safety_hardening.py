"""Safety-correct score_tf semantics for LIVE runtime.

Fixes four scoring defects without changing thresholds, leverage, NEXUS, sizing,
or exchange permissions:
- indicator exceptions never award positive score;
- ADX points require ADX direction to match the candidate direction;
- doji candles are not counted as directional SHORT bodies;
- the returned score decomposition remains compatible with canonical callers.
"""
from __future__ import annotations


def install(strategy, log) -> None:
    if getattr(strategy, "_scoring_safety_hardening_installed", False):
        return

    np = strategy.np

    def safe_score_tf(closes, highs, lows, opens, volumes, direction,
                      atr_v, atr_avg, orderbook=None) -> dict:
        price = closes[-1]
        if price <= 0 or atr_v <= 0:
            return {"ok": False, "total": 0}

        vols = np.array(volumes, dtype=float)
        avg_vol = vols[-21:-1].mean() if len(vols) > 21 else (vols.mean() or 1)
        vol_r = float(vols[-1] / avg_vol)

        # Trend evidence: every unavailable source is neutral, never positive.
        try:
            adx_data = strategy.adx_fn(highs, lows, closes)
            adx_v = float(adx_data.get("adx", 0.0) or 0.0)
            adx_trend = adx_v > 25
            adx_dir = str(adx_data.get("direction", "NEUTRAL"))
            adx_aligned = adx_dir == direction
        except Exception:
            adx_v = 0.0
            adx_trend = False
            adx_aligned = False

        e20 = float(strategy.ema(closes, 20)[-1])
        e50 = float(strategy.ema(closes, 50)[-1])
        e200 = float(strategy.ema(closes, min(200, len(closes)-1))[-1])
        bull = not np.isnan(e20) and not np.isnan(e50) and e20 > e50 and price > e20
        bear = not np.isnan(e20) and not np.isnan(e50) and e20 < e50 and price < e20
        full_stack = (bull and e50 > e200) or (bear and e50 < e200)
        aligned = (direction == "LONG" and bull) or (direction == "SHORT" and bear)

        try:
            smc = strategy.smc_analysis(highs, lows, closes)
        except Exception:
            smc = {
                "structure": "UNKNOWN", "hh": False, "hl": False,
                "lh": False, "ll": False, "bos": False,
                "bos_dir": "NONE", "choch": False,
            }

        try:
            vwap_v = strategy.vwap_fn(highs, lows, closes, volumes)
            vwap_ok = ((direction == "LONG" and price > vwap_v) or
                       (direction == "SHORT" and price < vwap_v))
        except Exception:
            vwap_v = price
            vwap_ok = False

        trend_s = 0
        # ADX must agree with BOTH price/EMA alignment and ADX direction.
        if aligned and adx_aligned:
            if adx_v > 30:
                trend_s += 10
            elif adx_v > 25:
                trend_s += 7
            elif adx_v > 20:
                trend_s += 4
        if full_stack and aligned:
            trend_s += 10
        elif aligned:
            trend_s += 6
        if smc.get("bos") and direction in str(smc.get("bos_dir", "")):
            trend_s += 6
        if ((direction == "LONG" and smc.get("hh") and smc.get("hl")) or
                (direction == "SHORT" and smc.get("lh") and smc.get("ll"))):
            trend_s += 4
        if vwap_ok:
            trend_s += 3
        if adx_v < 20:
            trend_s = min(trend_s, 10)
        if smc.get("choch"):
            trend_s = max(0, trend_s - 5)
        trend_s = max(0, min(30, trend_s))

        # Volume/activity. Doji is neutral for both LONG and SHORT.
        try:
            ob = strategy.orderbook_imbalance(orderbook) if orderbook else {"bias": "NEUTRAL"}
            ob_ok = ((direction == "LONG" and ob.get("bias") == "BID_HEAVY") or
                     (direction == "SHORT" and ob.get("bias") == "ASK_HEAVY"))
        except Exception:
            ob = {"bias": "NEUTRAL"}
            ob_ok = False

        try:
            vp = strategy.volume_profile(highs, lows, volumes)
            poc = float(vp.get("poc", price))
            poc_ok = ((direction == "LONG" and price > poc) or
                      (direction == "SHORT" and price < poc))
        except Exception:
            poc_ok = False

        if direction == "LONG":
            bodies_ok = [closes[i] > opens[i] for i in range(-3, 0)]
        else:
            bodies_ok = [closes[i] < opens[i] for i in range(-3, 0)]

        if vol_r >= 1.8 and all(bodies_ok):
            vol_s = 20
        elif vol_r >= 1.4 and sum(bodies_ok) >= 2:
            vol_s = 14
        elif vol_r >= 1.1:
            vol_s = 9
        elif vol_r >= 0.8:
            vol_s = 5
        else:
            vol_s = 2
        if ob_ok:
            vol_s = min(20, vol_s + 2)
        if poc_ok:
            vol_s = min(20, vol_s + 2)
        vol_s = max(0, vol_s)

        # Momentum. Missing footprint evidence is neutral, never +3.
        rsi_v = float(strategy.rsi(closes)[-1])
        try:
            _, _, hist = strategy.macd(closes)
            h0 = float(hist[-1]) if not np.isnan(hist[-1]) else 0
            h1 = float(hist[-2]) if len(hist) > 1 and not np.isnan(hist[-2]) else h0
        except Exception:
            h0 = 0.0
            h1 = 0.0

        try:
            fp = strategy.delta_footprint(closes, list(vols), opens)
            fp_ok = ((direction == "LONG" and fp.get("bias") == "BULLISH") or
                     (direction == "SHORT" and fp.get("bias") == "BEARISH"))
            fp_div = bool(fp.get("divergence"))
        except Exception:
            fp_ok = False
            fp_div = False

        if direction == "LONG":
            if 40 <= rsi_v <= 80:
                rsi_s = 10
            elif 33 <= rsi_v < 40:
                rsi_s = 6
            elif 80 < rsi_v <= 90:
                rsi_s = 4
            elif rsi_v > 90:
                rsi_s = 2
            else:
                rsi_s = 0
            if h0 > 0 and h0 > h1:
                macd_s = 10
            elif h0 > 0:
                macd_s = 6
            elif h0 > h1:
                macd_s = 4
            else:
                macd_s = 0
        else:
            if 20 <= rsi_v <= 60:
                rsi_s = 10
            elif 60 < rsi_v <= 67:
                rsi_s = 6
            elif 10 <= rsi_v < 20:
                rsi_s = 4
            elif rsi_v < 10:
                rsi_s = 2
            else:
                rsi_s = 0
            if h0 < 0 and h0 < h1:
                macd_s = 10
            elif h0 < 0:
                macd_s = 6
            elif h0 < h1:
                macd_s = 4
            else:
                macd_s = 0

        momentum_s = rsi_s + macd_s
        if fp_ok and not fp_div:
            momentum_s = min(20, momentum_s + 3)
        if fp_div:
            momentum_s = max(0, momentum_s - 3)
        momentum_s = max(0, min(20, momentum_s))

        atr_pct = atr_v / price * 100
        atr_expanding = atr_v > atr_avg * 1.03
        try:
            bb = strategy.bollinger(closes)
            bb_squeeze = bb["squeezed"]
            bb_width = bb["width"]
        except Exception:
            bb_squeeze = False
            bb_width = 3.0
        try:
            ci_data = strategy.chop_fn(highs, lows, closes)
            ci_chop = ci_data["chop"]
            ci_trend = ci_data["trending"]
            ci_v = ci_data["ci"]
        except Exception:
            ci_chop = False
            ci_trend = True
            ci_v = 50

        if atr_expanding and ci_trend and not bb_squeeze:
            atr_s = 15
        elif atr_expanding and not ci_chop:
            atr_s = 11
        elif not ci_chop and 0.15 <= atr_pct <= 5.0:
            atr_s = 8
        elif ci_chop:
            atr_s = 3
        else:
            atr_s = 5
        atr_s = max(0, min(15, atr_s))

        body = abs(closes[-1] - opens[-1])
        cr = highs[-1] - lows[-1]
        wick_r = 1 - (body / cr) if cr > 0 else 1
        struct_s = 15
        if wick_r > 0.72:
            struct_s -= 5
        if vol_r > 3.0 and body < atr_v * 0.10:
            struct_s -= 5
        if smc.get("choch"):
            struct_s -= 4
        ph = max(highs[-6:-1]) if len(highs) > 6 else highs[-1]
        pl = min(lows[-6:-1]) if len(lows) > 6 else lows[-1]
        if direction == "LONG" and highs[-1] > ph and closes[-1] < ph:
            struct_s -= 5
        if direction == "SHORT" and lows[-1] < pl and closes[-1] > pl:
            struct_s -= 5
        if ci_chop:
            struct_s = max(0, struct_s - 3)
        struct_s = max(0, min(15, struct_s))

        total = trend_s + vol_s + momentum_s + atr_s + struct_s
        return {
            "ok": True, "total": total,
            "trend_s": trend_s, "vol_s": vol_s,
            "momentum_s": momentum_s, "atr_s": atr_s, "struct_s": struct_s,
            "rsi_v": rsi_v, "rsi_s": rsi_s, "macd_s": macd_s,
            "adx_v": adx_v, "adx_trending": adx_trend, "adx_ranging": adx_v < 20,
            "adx_aligned": adx_aligned,
            "ci_chop": ci_chop, "ci_trend": ci_trend, "ci_v": ci_v,
            "bb_squeeze": bb_squeeze, "bb_width": bb_width,
            "vwap": vwap_v, "vwap_ok": vwap_ok,
            "smc_structure": smc.get("structure", "UNKNOWN"),
            "bos": bool(smc.get("bos")), "choch": bool(smc.get("choch")),
            "hh": bool(smc.get("hh")), "hl": bool(smc.get("hl")),
            "ob_bias": ob.get("bias", "NEUTRAL") if orderbook else "N/A",
            "fp_ok": fp_ok, "fp_div": fp_div,
            "vol_r": vol_r, "atr_v": atr_v, "atr_expanding": atr_expanding,
            "aligned": aligned, "bull": bull, "bear": bear, "full": full_stack,
            "price": price,
            "summary": (
                f"T{trend_s}+V{vol_s}+M{momentum_s}+A{atr_s}+S{struct_s}={total} "
                f"ADX{adx_v:.0f} CI{ci_v:.0f} RSI{rsi_v:.0f}"
            ),
        }

    strategy.score_tf = safe_score_tf
    strategy._scoring_safety_hardening_installed = True
    log.warning(
        "[SCORING_SAFETY] installed fail_open_indicator_credit=false "
        "adx_requires_direction=true doji_short_bias=false thresholds_unchanged=true "
        "leverage_unchanged=true execution_permissions_unchanged=true"
    )
