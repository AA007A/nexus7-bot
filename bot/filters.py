"""KAKAZITO TRADE — Filters: Funding, OI Delta, Fear&Greed, Horário, Macro. Fault-tolerant."""
import asyncio, time, aiohttp
from datetime import datetime, timezone
from bot.logger import log

_cache = {"fear_greed": {"value": 50, "label": "Neutral", "ts": 0}, "macro_events": {"events": [], "ts": 0}, "oi_history": {}}
FUNDING_BLOCK_LONG=0.0005; FUNDING_BLOCK_SHORT=-0.0005; LIQUIDITY_HOURS=(6,23); SPREAD_MAX_PCT=0.0005; MACRO_BLOCK_MIN=30

async def check_funding(client, symbol, direction):
    try: fr = await client.get_funding_rate(symbol)
    except: return {"ok": True, "funding": 0.0, "reason": "funding N/A"}
    if direction=="LONG" and fr>FUNDING_BLOCK_LONG: return {"ok":False,"funding":fr,"reason":f"Funding {fr*100:.4f}%>+0.05% bloqueia LONG"}
    if direction=="SHORT" and fr<FUNDING_BLOCK_SHORT: return {"ok":False,"funding":fr,"reason":f"Funding {fr*100:.4f}%<-0.05% bloqueia SHORT"}
    return {"ok":True,"funding":fr,"reason":f"Funding {fr*100:.4f}% OK"}

async def check_oi_delta(client, symbol, direction):
    try:
        d=await client.get_open_interest(symbol); oi=float(d.get("openInterest",0))
    except: return {"ok":True,"oi":0,"delta":0,"reason":"OI N/A"}
    hist=_cache["oi_history"].setdefault(symbol,[])
    hist.append(oi)
    if len(hist)>5: hist.pop(0)
    if len(hist)<2: return {"ok":True,"oi":oi,"delta":0,"reason":"OI histórico insuficiente"}
    delta=(oi-hist[-2])/hist[-2] if hist[-2]>0 else 0
    if delta<-0.005: return {"ok":False,"oi":oi,"delta":delta,"reason":f"OI caindo {delta*100:.2f}% — sem convicção"}
    return {"ok":True,"oi":oi,"delta":delta,"reason":f"OI {delta*100:.2f}% OK"}

async def update_fear_greed():
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get("https://api.alternative.me/fng/?limit=1",timeout=aiohttp.ClientTimeout(total=8)) as r:
                    d=await r.json(); val=int(d["data"][0]["value"]); label=d["data"][0]["value_classification"]
                    _cache["fear_greed"]={"value":val,"label":label,"ts":time.time()}
                    log.info(f"📊 Fear&Greed: {val} ({label})")
        except Exception as e: log.debug(f"fear_greed: {e}")
        await asyncio.sleep(3600)

def check_fear_greed(direction):
    fg=_cache["fear_greed"]; val=fg["value"]
    if val<=25 and direction=="SHORT": return {"ok":False,"value":val,"label":fg["label"],"reason":f"Extremo MEDO({val}) só LONGs"}
    if val>=75 and direction=="LONG": return {"ok":False,"value":val,"label":fg["label"],"reason":f"Extremo GANÂNCIA({val}) só SHORTs"}
    return {"ok":True,"value":val,"label":fg["label"],"reason":f"F&G {val} OK"}

def check_trading_hours():
    now=datetime.now(timezone.utc); h=now.hour; wd=now.weekday()>=5
    if not (LIQUIDITY_HOURS[0]<=h<LIQUIDITY_HOURS[1]): return {"ok":False,"hour":h,"weekend":wd,"size_mult":0.0,"reason":f"Fora horário ({h:02d}h UTC)"}
    return {"ok":True,"hour":h,"weekend":wd,"size_mult":0.6 if wd else 1.0,"reason":f"Horário OK{' FDS 60%' if wd else ''}"}

async def check_spread(client, symbol):
    try:
        ob=await client.get_orderbook(symbol); bids=ob.get("b",[["0","0"]]); asks=ob.get("a",[["0","0"]])
        bid=float(bids[0][0]); ask=float(asks[0][0]); mid=(bid+ask)/2
        sp=(ask-bid)/mid if mid>0 else 0
        if sp>SPREAD_MAX_PCT: return {"ok":False,"spread":sp,"reason":f"Spread {sp*100:.4f}%>0.05%"}
        return {"ok":True,"spread":sp,"reason":f"Spread {sp*100:.4f}% OK"}
    except: return {"ok":True,"spread":0,"reason":"orderbook N/A"}

async def update_macro_events():
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json",timeout=aiohttp.ClientTimeout(total=10),headers={"User-Agent":"Mozilla/5.0"}) as r:
                    evs=await r.json(); high=[e for e in evs if e.get("impact","").lower()=="high" and e.get("country","").upper()=="USD"]
                    _cache["macro_events"]={"events":high,"ts":time.time()}; log.info(f"📅 {len(high)} eventos macro")
        except Exception as e: log.debug(f"macro_events: {e}")
        await asyncio.sleep(21600)

def check_macro_events():
    now=datetime.now(timezone.utc); events=_cache["macro_events"].get("events",[])
    for ev in events:
        try:
            ev_dt=datetime.fromisoformat(ev.get("date","").replace("Z","+00:00"))
            if ev_dt.tzinfo and abs((now-ev_dt).total_seconds())/60<=MACRO_BLOCK_MIN:
                return {"ok":False,"event":ev.get("title","Evento"),"reason":f"Evento macro próximo"}
        except: continue
    return {"ok":True,"event":None,"reason":"Sem eventos macro próximos"}

async def run_all_filters(client, symbol, direction):
    res={}
    for fn,kw in [(check_trading_hours,{}),(check_macro_events,{}),(check_fear_greed,{"direction":direction})]:
        r=fn(**kw); key=fn.__name__.replace("check_",""); res[key]=r
        if not r["ok"]: return {"ok":False,"blocked_by":key,"size_mult":0.0,"details":res}
    for fn,kw in [(check_funding,{"client":client,"symbol":symbol,"direction":direction}),(check_oi_delta,{"client":client,"symbol":symbol,"direction":direction}),(check_spread,{"client":client,"symbol":symbol})]:
        r=await fn(**kw); key=fn.__name__.replace("check_",""); res[key]=r
        if not r["ok"]: return {"ok":False,"blocked_by":key,"size_mult":0.0,"details":res}
    return {"ok":True,"blocked_by":None,"size_mult":res.get("trading_hours",{}).get("size_mult",1.0),"details":res}

def get_filter_summary():
    now=datetime.now(timezone.utc)
    return {"fear_greed":_cache["fear_greed"],"macro_events":[e.get("title","?") for e in _cache["macro_events"].get("events",[])[:3]],"trading_hours":LIQUIDITY_HOURS[0]<=now.hour<LIQUIDITY_HOURS[1],"current_hour":now.hour}
