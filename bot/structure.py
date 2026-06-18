"""BGX Capital — Structure Analysis: OB, FVG, BOS/CHoCH, Stop Hunt, MTF Confluence"""
import numpy as np
from bot.logger import log


def detect_order_blocks(closes, highs, lows, opens, lookback=20) -> dict:
    if len(closes) < lookback + 3:
        return {"bull_ob": None, "bear_ob": None, "price_in_ob": False, "ob_type": None}
    price = closes[-1]
    bull_obs, bear_obs = [], []
    for i in range(3, min(lookback, len(closes) - 3)):
        idx = -(i + 3)
        try:
            is_bear = closes[idx] < opens[idx]
            n3 = [closes[idx+j] > opens[idx+j] for j in range(1,4) if abs(idx+j) < len(closes) and (idx+j) != 0]
            if is_bear and len(n3) == 3 and all(n3):
                bull_obs.append({"high": highs[idx], "low": lows[idx]})
            is_bull = closes[idx] > opens[idx]
            n3b = [closes[idx+j] < opens[idx+j] for j in range(1,4) if abs(idx+j) < len(closes) and (idx+j) != 0]
            if is_bull and len(n3b) == 3 and all(n3b):
                bear_obs.append({"high": highs[idx], "low": lows[idx]})
        except IndexError:
            continue
    bull_ob = bull_obs[0] if bull_obs else None
    bear_ob = bear_obs[0] if bear_obs else None
    in_bull = bool(bull_ob and bull_ob["low"] <= price <= bull_ob["high"])
    in_bear = bool(bear_ob and bear_ob["low"] <= price <= bear_ob["high"])
    return {"bull_ob": bull_ob, "bear_ob": bear_ob,
            "price_in_ob": in_bull or in_bear,
            "ob_type": "BULL" if in_bull else ("BEAR" if in_bear else None)}


def detect_fvg(closes, highs, lows, lookback=30) -> dict:
    if len(closes) < 3:
        return {"bull_fvg": [], "bear_fvg": [], "price_in_fvg": False, "fvg_type": None}
    price = closes[-1]; bull_fvgs = []; bear_fvgs = []
    for i in range(2, min(lookback + 2, len(closes))):
        try:
            if highs[-i-2] < lows[-i]:
                bull_fvgs.append({"low": highs[-i-2], "high": lows[-i], "mid": (highs[-i-2]+lows[-i])/2})
            if lows[-i-2] > highs[-i]:
                bear_fvgs.append({"low": highs[-i], "high": lows[-i-2], "mid": (highs[-i]+lows[-i-2])/2})
        except IndexError:
            continue
    in_bull = any(f["low"] <= price <= f["high"] for f in bull_fvgs)
    in_bear = any(f["low"] <= price <= f["high"] for f in bear_fvgs)
    return {"bull_fvg": bull_fvgs[:3], "bear_fvg": bear_fvgs[:3],
            "price_in_fvg": in_bull or in_bear,
            "fvg_type": "BULL" if in_bull else ("BEAR" if in_bear else None)}


def detect_bos_choch(closes, highs, lows, lookback=30) -> dict:
    if len(closes) < 10:
        return {"bos": False, "choch": False, "structure": "UNKNOWN", "direction": None,
                "bos_dir": None, "hh": False, "hl": False, "lh": False, "ll": False,
                "last_swing_high": 0.0, "last_swing_low": 0.0}
    h = highs[-min(lookback, len(highs)):]; l = lows[-min(lookback, len(lows)):]; c = closes[-min(lookback, len(closes)):]
    step = max(3, len(h) // 6)
    ph = [max(h[i:i+step]) for i in range(0, len(h)-step, step)]
    pl = [min(l[i:i+step]) for i in range(0, len(l)-step, step)]
    hh = ph[-1] > ph[-2] if len(ph) >= 2 else False
    lh = ph[-1] < ph[-2] if len(ph) >= 2 else False
    hl = pl[-1] > pl[-2] if len(pl) >= 2 else False
    ll = pl[-1] < pl[-2] if len(pl) >= 2 else False
    if hh and hl: structure = "UPTREND"
    elif lh and ll: structure = "DOWNTREND"
    else: structure = "RANGING"
    last_sh = max(h[-min(6,len(h)):-1]) if len(h) > 1 else h[-1]
    last_sl = min(l[-min(6,len(l)):-1]) if len(l) > 1 else l[-1]
    bull_bos = c[-1] > last_sh; bear_bos = c[-1] < last_sl
    bos_dir = "LONG" if bull_bos else ("SHORT" if bear_bos else None)
    choch = (structure == "UPTREND" and bear_bos) or (structure == "DOWNTREND" and bull_bos)
    return {"bos": bull_bos or bear_bos, "bos_dir": bos_dir, "choch": choch,
            "structure": structure, "direction": "LONG" if structure=="UPTREND" else ("SHORT" if structure=="DOWNTREND" else None),
            "hh": hh, "hl": hl, "lh": lh, "ll": ll,
            "last_swing_high": round(last_sh, 4), "last_swing_low": round(last_sl, 4)}


def detect_stop_hunt(closes, highs, lows, opens) -> dict:
    if len(closes) < 3:
        return {"stop_hunt": False, "direction": None, "confirmed": False, "wait_confirmation": False, "wick_ratio": 0}
    body = abs(closes[-2] - opens[-2])
    wick_up = highs[-2] - max(closes[-2], opens[-2])
    wick_dn = min(closes[-2], opens[-2]) - lows[-2]
    if body <= 0:
        return {"stop_hunt": False, "direction": None, "confirmed": False, "wait_confirmation": False, "wick_ratio": 0}
    bull_hunt = wick_dn > body * 2; bear_hunt = wick_up > body * 2
    hunt = bull_hunt or bear_hunt
    confirmed = (bull_hunt and closes[-1] > closes[-2]) or (bear_hunt and closes[-1] < closes[-2])
    direction = "LONG" if bull_hunt else ("SHORT" if bear_hunt else None)
    return {"stop_hunt": hunt, "direction": direction, "confirmed": confirmed,
            "wait_confirmation": hunt and not confirmed,
            "wick_ratio": round(max(wick_up, wick_dn) / body, 2)}


def mtf_entry_signal(k_1h, k_15m, k_5m=None) -> dict:
    """
    Confluência Multi-Timeframe: 1H bias → 15M setup.
    k_5m é opcional — engine não coleta 5M por padrão.
    Quando k_5m não disponível, usa confirmação do 15M como micro-entrada.
    """
    if len(k_1h) < 10 or len(k_15m) < 10:
        return {"aligned": False, "direction": None, "reason": "dados insuficientes"}

    def ga(kl):
        return (
            [k["c"] for k in kl], [k["h"] for k in kl],
            [k["l"] for k in kl], [k["o"] for k in kl]
        )

    c1h, h1h, l1h, o1h = ga(k_1h)
    c15, h15, l15, o15 = ga(k_15m)

    from bot.indicators import ema
    e20 = float(ema(c1h, 20)[-1])
    e50 = float(ema(c1h, min(50, len(c1h) - 1))[-1])
    bos1h = detect_bos_choch(c1h, h1h, l1h)

    if   e20 > e50 and c1h[-1] > e20: bias = "LONG"
    elif e20 < e50 and c1h[-1] < e20: bias = "SHORT"
    else: return {"aligned": False, "direction": None, "reason": "1H sem bias claro"}

    if bos1h["choch"]:
        return {"aligned": False, "direction": None, "reason": "1H CHoCH — estrutura quebrada"}

    ob15  = detect_order_blocks(c15, h15, l15, o15)
    fvg15 = detect_fvg(c15, h15, l15)
    bos15 = detect_bos_choch(c15, h15, l15)
    hunt15 = detect_stop_hunt(c15, h15, l15, o15)

    ob_ok  = ob15["price_in_ob"]  and ob15["ob_type"]  == ("BULL" if bias == "LONG" else "BEAR")
    fvg_ok = fvg15["price_in_fvg"] and fvg15["fvg_type"] == ("BULL" if bias == "LONG" else "BEAR")
    bos_ok = bos15["bos_dir"] == bias

    if not (ob_ok or fvg_ok or bos_ok):
        return {"aligned": False, "direction": None, "reason": "15M sem setup (OB/FVG/BOS)"}

    # ── Confirmação de micro-entrada ──────────────────────────────
    # Usa 5M se disponível, caso contrário usa últimos 10 candles do 15M como proxy
    if k_5m and len(k_5m) >= 5:
        c5, h5, l5, o5 = ga(k_5m)
    else:
        # Proxy: últimos 10 candles do 15M simulam resolução mais fina
        c5, h5, l5, o5 = (c15[-10:], h15[-10:], l15[-10:], o15[-10:])

    hunt_micro = detect_stop_hunt(c5, h5, l5, o5)
    if hunt_micro["wait_confirmation"]:
        return {"aligned": False, "direction": None,
                "reason": "Stop hunt não confirmado na micro-entrada"}

    e20_micro = float(ema(c5, min(20, len(c5) - 1))[-1])
    if bias == "LONG"  and c5[-1] < e20_micro:
        return {"aligned": False, "direction": None, "reason": "Micro abaixo EMA20"}
    if bias == "SHORT" and c5[-1] > e20_micro:
        return {"aligned": False, "direction": None, "reason": "Micro acima EMA20"}

    setup = "OB" if ob_ok else ("FVG" if fvg_ok else "BOS")
    tf_micro = "5M" if (k_5m and len(k_5m) >= 5) else "15M_proxy"
    log.info(f"MTF ✅ {bias} | 15M:{setup} micro:{tf_micro}:OK")

    return {
        "aligned":    True,
        "direction":  bias,
        "setup_type": setup,
        "ob":         ob15,
        "fvg":        fvg15,
        "bos":        bos15,
        "stop_hunt":  hunt_micro,
        "reason":     f"1H:{bias} 15M:{setup} micro:{tf_micro}:OK",
    }
