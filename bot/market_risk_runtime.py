"""Live market-risk collectors for NEXUS-7.

CoinGlass is optional. Cross-asset macro prices use a free public chart source
and are strictly fail-neutral. No external signal can authorize execution.
"""
from __future__ import annotations
import asyncio, os, time
from typing import Any
import aiohttp
from bot.market_risk_intelligence import assess_market_risk, compact_risk_log

_SIGNAL_TTL_S={"liquidation_usd_1h":1200.0,"open_interest_change_pct":1800.0,"funding_rate_pct":1800.0,"spx_change_pct":1800.0,"ndx_change_pct":1800.0,"vix_change_pct":1800.0,"dxy_change_pct":1800.0,"us2y_yield_change_bps":21600.0,"us10y_yield_change_bps":21600.0,"macro_event_severity":1800.0}
_state:dict[str,Any]={"signals":{},"signal_updated_at":{},"providers":{}}
_previous_coinglass_oi:tuple[float,float]|None=None

def snapshot(now:float|None=None)->dict[str,Any]:
 current=time.time() if now is None else float(now); raw=dict(_state.get("signals",{}) or {}); timestamps=dict(_state.get("signal_updated_at",{}) or {}); signals={}; ages={}
 for key,value in raw.items():
  ts=float(timestamps.get(key,0) or 0); ttl=float(_SIGNAL_TTL_S.get(key,1800.0)); age=current-ts if ts>0 else float("inf")
  if ts>0 and -5.0<=age<=ttl: signals[key]=value; ages[key]=max(0.0,age)
 assessment=assess_market_risk(signals)
 return {"fresh":bool(signals),"signals":signals,"signal_ages_s":ages,"providers":dict(_state.get("providers",{}) or {}),"assessment":assessment}

def _mark_provider(name:str,status:str)->None: _state.setdefault("providers",{})[name]=status

def _merge_signals(values:dict[str,Any],now:float|None=None)->None:
 clean={k:v for k,v in values.items() if v is not None}
 if not clean:return
 ts=time.time() if now is None else float(now); _state.setdefault("signals",{}).update(clean); signal_ts=_state.setdefault("signal_updated_at",{})
 for key in clean: signal_ts[key]=ts

def parse_coinglass_liquidation(payload:dict[str,Any])->dict[str,float]:
 if str(payload.get("code",""))!="0":return {}
 for row in payload.get("data") or []:
  if str((row or {}).get("exchange","")).casefold()=="all":
   try:return {"liquidation_usd_1h":max(0.0,float(row.get("liquidation_usd",0) or 0))}
   except (TypeError,ValueError,OverflowError):return {}
 return {}

def parse_coinglass_markets(payload:dict[str,Any],now:float|None=None)->dict[str,float]:
 global _previous_coinglass_oi
 if str(payload.get("code",""))!="0":return {}
 btc=next((r for r in payload.get("data") or [] if str((r or {}).get("symbol","")).upper()=="BTC"),None)
 if not btc:return {}
 out={}
 try:out["funding_rate_pct"]=float(btc.get("avg_funding_rate_by_oi",0) or 0)
 except (TypeError,ValueError,OverflowError):pass
 try:
  oi=float(btc.get("open_interest_usd",0) or 0); ts=time.time() if now is None else float(now); prev=_previous_coinglass_oi
  if oi>0 and prev and prev[0]>0 and ts>prev[1]:out["open_interest_change_pct"]=((oi/prev[0])-1.0)*100.0
  if oi>0:_previous_coinglass_oi=(oi,ts)
 except (TypeError,ValueError,OverflowError):pass
 return out

def parse_chart_change(payload:dict[str,Any],yield_bps:bool=False)->float|None:
 try:
  result=payload["chart"]["result"][0]; closes=[float(x) for x in result["indicators"]["quote"][0]["close"] if x is not None]
  if len(closes)<2 or closes[-2]==0:return None
  return (closes[-1]-closes[-2])*100.0 if yield_bps else ((closes[-1]/closes[-2])-1.0)*100.0
 except (KeyError,IndexError,TypeError,ValueError,OverflowError):return None

async def _coinglass_loop(log)->None:
 key=os.environ.get("COINGLASS_API_KEY","").strip()
 if not key:_mark_provider("coinglass","disabled_no_key");return
 poll_s=max(60.0,float(os.environ.get("COINGLASS_POLL_SECONDS","14400") or 14400)); headers={"CG-API-KEY":key,"accept":"application/json"}; urls=("https://open-api-v4.coinglass.com/api/futures/liquidation/exchange-list?symbol=BTC&range=1h","https://open-api-v4.coinglass.com/api/futures/coins-markets?per_page=20&page=1")
 while True:
  try:
   statuses=[]
   async with aiohttp.ClientSession(headers=headers) as session:
    async with session.get(urls[0],timeout=aiohttp.ClientTimeout(total=10)) as r:
     statuses.append(r.status)
     if r.status==200:_merge_signals(parse_coinglass_liquidation(await r.json(content_type=None)))
    async with session.get(urls[1],timeout=aiohttp.ClientTimeout(total=10)) as r:
     statuses.append(r.status)
     if r.status==200:_merge_signals(parse_coinglass_markets(await r.json(content_type=None)))
   _mark_provider("coinglass","ok" if all(s==200 for s in statuses) else f"http_{statuses}_fail_neutral")
  except Exception as exc:_mark_provider("coinglass",f"unavailable:{type(exc).__name__}");log.warning("[MARKET_RISK_SOURCE] provider=coinglass unavailable=%s fail_neutral=true",type(exc).__name__)
  await asyncio.sleep(poll_s)

async def _cross_asset_loop(log)->None:
 # Yahoo symbols: S&P500, Nasdaq-100, VIX, DXY futures/index proxy, US 2Y, US 10Y.
 symbols={"spx_change_pct":"^GSPC","ndx_change_pct":"^NDX","vix_change_pct":"^VIX","dxy_change_pct":"DX-Y.NYB","us2y_yield_change_bps":"^IRX","us10y_yield_change_bps":"^TNX"}
 headers={"User-Agent":"Mozilla/5.0"}
 while True:
  ok=0; values={}
  try:
   async with aiohttp.ClientSession(headers=headers) as session:
    for key,symbol in symbols.items():
     url=f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
     try:
      async with session.get(url,timeout=aiohttp.ClientTimeout(total=8)) as r:
       if r.status==200:
        value=parse_chart_change(await r.json(content_type=None),yield_bps=key.endswith("_bps"))
        if value is not None:values[key]=value;ok+=1
     except Exception as exc:log.debug("[CROSS_ASSET] symbol=%s unavailable=%s fail_neutral=true",symbol,type(exc).__name__)
   _merge_signals(values);_mark_provider("cross_asset_macro",f"ok:{ok}/{len(symbols)}" if ok else "unavailable_fail_neutral")
  except Exception as exc:_mark_provider("cross_asset_macro",f"unavailable:{type(exc).__name__}")
  await asyncio.sleep(900)

async def market_risk_reader_loop(log)->None:
 log.info("[MARKET_RISK_SOURCES] providers=CoinGlass,cross_asset_macro(SPX,NDX,VIX,DXY,US2Y,US10Y),public_macro_news; execution_effect=NONE")
 tasks=[asyncio.create_task(_coinglass_loop(log)),asyncio.create_task(_cross_asset_loop(log))]
 try:
  while True:
   snap=snapshot();log.info("%s providers=%s fresh_signals=%s execution_effect=NONE",compact_risk_log(snap["assessment"]),snap["providers"],sorted(snap["signals"]));await asyncio.sleep(120)
 finally:
  for task in tasks:task.cancel()

def install(PilotGuard,scoring,log)->None:
 if getattr(PilotGuard,"_market_risk_intelligence_installed",False):return
 original_news_loop=scoring.news_reader_loop
 async def combined_reader_loop():await asyncio.gather(original_news_loop(),market_risk_reader_loop(log))
 original_evaluate=PilotGuard.evaluate
 def evaluate_with_market_risk(self,engine,client,symbol,ai_decision=None):
  reasons=list(original_evaluate(self,engine,client,symbol,ai_decision));snap=snapshot();assessment=snap["assessment"]
  if snap["fresh"] and assessment.block_new_entries:reasons.append(f"15_MARKET_RISK: EXTREME score={assessment.score} signals={','.join(assessment.reasons[:5]) or 'NONE'}")
  self.state.blocked_reasons=reasons;return reasons
 scoring.news_reader_loop=combined_reader_loop;PilotGuard.evaluate=evaluate_with_market_risk;PilotGuard._market_risk_intelligence_installed=True
 log.warning("[MARKET_RISK_INTELLIGENCE] installed: CoinGlass + SPX/NDX/VIX/DXY/US2Y/US10Y + structured US macro/news; EXTREME combined risk blocks pilot entries; external signals never authorize execution")
