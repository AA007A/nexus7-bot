"""KAKAZITO TRADE — Regime Classifier: TRENDING/RANGING/CHOPPY"""
import numpy as np
from bot.logger import log

def classify(closes,highs,lows,symbol=""):
    if len(closes)<20: return {"regime":"UNKNOWN","strategy":"HOLD","adx":0,"ci":50,"atr_pct":0,"tradeable":False}
    try:
        from bot.indicators import adx as af; d=af(highs,lows,closes); adx_v=d["adx"]; adx_dir=d["direction"]
    except: adx_v=20; adx_dir="LONG"
    try:
        from bot.indicators import atr as atrf; import numpy as np
        a=atrf(highs,lows,closes); av=float(a[-1]); avg=float(np.mean(a[-14:])) if len(a)>=14 else av
        atr_expand=av>avg*1.03; atr_pct=av/closes[-1]*100
    except: av=0; avg=0; atr_expand=False; atr_pct=0
    try:
        from bot.indicators import choppiness as cf; cd=cf(highs,lows,closes); ci_chop=cd["chop"]; ci_v=cd["ci"]
    except: ci_chop=False; ci_v=50
    try:
        from bot.indicators import bollinger; bb=bollinger(closes); bb_sq=bb["squeezed"]; bb_w=bb["width"]
    except: bb_sq=False; bb_w=3.0
    if (avg>0 and av<avg*0.70) or (ci_chop and adx_v<18): regime="CHOPPY"; strategy="HOLD"
    elif adx_v>25 and atr_expand and not ci_chop: regime="TRENDING_UP" if adx_dir=="LONG" else "TRENDING_DOWN"; strategy="TREND_FOLLOW"
    elif adx_v<20: regime="RANGING"; strategy="MEAN_REVERSION"
    else: regime="NEUTRAL"; strategy="SELECTIVE"
    if symbol: log.debug(f"[{symbol}] Regime={regime} ADX={adx_v:.1f} CI={ci_v:.1f}")
    return {"regime":regime,"strategy":strategy,"adx":round(adx_v,1),"adx_dir":adx_dir,
            "ci":round(ci_v,1),"atr_pct":round(atr_pct,3),"atr_expand":atr_expand,
            "bb_squeeze":bb_sq,"bb_width":round(bb_w,2),"tradeable":regime!="CHOPPY"}
