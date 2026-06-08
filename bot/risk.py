"""KAKAZITO TRADE — Risk: Partial TPs (40/35/25%), Trailing Stop, Sizing 1% risco."""
import time
from dataclasses import dataclass, field
from bot.logger import log
from bot.config import cfg

TP1_PCT=0.40; TP2_PCT=0.35; TP3_PCT=0.25
MAX_RISK_PCT=0.01; MAX_DRAWDOWN=0.08

@dataclass
class PositionRisk:
    symbol:str; direction:str; entry:float; sl:float
    tp1:float; tp2:float; tp3:float; qty_total:float
    qty_remain:float=field(init=False); tp1_hit:bool=False; tp2_hit:bool=False
    be_set:bool=False; trailing_sl:float=0.0; opened_at:float=field(default_factory=time.time)
    def __post_init__(self): self.qty_remain=self.qty_total; self.trailing_sl=self.sl
    def r_value(self): return abs(self.entry-self.sl)
    def to_dict(self):
        return {"symbol":self.symbol,"direction":self.direction,"entry":round(self.entry,6),
                "sl":round(self.trailing_sl,6),"tp1":round(self.tp1,6),"tp2":round(self.tp2,6),
                "tp3":round(self.tp3,6),"qty_total":self.qty_total,"qty_remain":round(self.qty_remain,8),
                "tp1_hit":self.tp1_hit,"tp2_hit":self.tp2_hit,"be_set":self.be_set,"trailing_sl":round(self.trailing_sl,6)}

def build_position_risk(symbol,direction,entry,sl,qty):
    r=abs(entry-sl)
    tp1=entry+(r if direction=="LONG" else -r)
    tp2=entry+(r*2 if direction=="LONG" else -r*2)
    tp3=entry+(r*3 if direction=="LONG" else -r*3)
    return PositionRisk(symbol=symbol,direction=direction,entry=entry,sl=sl,tp1=tp1,tp2=tp2,tp3=tp3,qty_total=qty)

def calc_position_size(balance,entry,sl,leverage=10,size_mult=1.0):
    if entry<=0 or sl<=0: return 0.0
    risk_usdt=balance*MAX_RISK_PCT*size_mult
    sl_pct=abs(entry-sl)/entry
    return round(max(risk_usdt/(sl_pct*entry) if sl_pct>0 else 0,0.001),6) if sl_pct>0 else 0.0

async def check_partial_tps(pos:PositionRisk,cur:float,client)->dict:
    actions=[]; r=pos.r_value()
    if r<=0: return {"actions":actions}
    if not pos.tp1_hit:
        hit=(pos.direction=="LONG" and cur>=pos.tp1) or (pos.direction=="SHORT" and cur<=pos.tp1)
        if hit:
            q=round(pos.qty_total*TP1_PCT,8)
            try:
                await client.place_order(pos.symbol,"Sell" if pos.direction=="LONG" else "Buy",q)
                pos.qty_remain-=q; pos.tp1_hit=True; pos.trailing_sl=pos.entry; pos.be_set=True
                await client.set_sl(pos.symbol,pos.entry)
                log.info(f"✅ TP1 {pos.symbol}: {q:.6f} @ {cur:.4f} | BE={pos.entry:.4f}"); actions.append("TP1")
            except Exception as e: log.error(f"TP1 {pos.symbol}: {e}")
    elif not pos.tp2_hit:
        hit=(pos.direction=="LONG" and cur>=pos.tp2) or (pos.direction=="SHORT" and cur<=pos.tp2)
        if hit:
            q=round(pos.qty_total*TP2_PCT,8)
            try:
                await client.place_order(pos.symbol,"Sell" if pos.direction=="LONG" else "Buy",q)
                pos.qty_remain-=q; pos.tp2_hit=True
                log.info(f"✅ TP2 {pos.symbol}: {q:.6f} @ {cur:.4f}"); actions.append("TP2")
            except Exception as e: log.error(f"TP2 {pos.symbol}: {e}")
    if pos.tp2_hit:
        new_sl=cur-(r*0.5) if pos.direction=="LONG" else cur+(r*0.5)
        better=(pos.direction=="LONG" and new_sl>pos.trailing_sl) or (pos.direction=="SHORT" and new_sl<pos.trailing_sl)
        if better:
            pos.trailing_sl=new_sl
            try: await client.set_sl(pos.symbol,new_sl); log.info(f"🔄 Trailing SL {pos.symbol}→{new_sl:.4f}"); actions.append("TRAIL_SL")
            except Exception as e: log.error(f"TrailSL {pos.symbol}: {e}")
    return {"actions":actions,"qty_remain":pos.qty_remain}

class RiskManager:
    def __init__(self): self.peak_balance=0.0; self.balance=0.0; self.drawdown=0.0; self._ready=False; self.positions={}
    def init(self,bal):
        if not self._ready and bal>0: self.peak_balance=bal; self.balance=bal; self._ready=True; log.info(f"📊 RiskManager: ${bal:.2f} | poder=${bal*cfg.LEVERAGE:.2f}")
    def update(self,bal):
        if bal<=0: return
        self.balance=bal; self.peak_balance=max(self.peak_balance,bal)
        self.drawdown=(self.peak_balance-bal)/self.peak_balance if self.peak_balance>0 else 0.0
    def can_open(self,n):
        if not self._ready: return False
        if self.drawdown>=MAX_DRAWDOWN: log.warning(f"🚨 Drawdown {self.drawdown:.1%}≥8%→bloqueado"); return False
        return n<cfg.MAX_POSITIONS
    def size(self,symbol,entry,sl,instruments,size_mult=1.0):
        if not self._ready or entry<=0 or sl<=0: return 0.0
        info=instruments.get(symbol,{}); min_qty=info.get("minQty",0.001); qty_step=info.get("qtyStep",0.001)
        qty=calc_position_size(self.balance,entry,sl,leverage=cfg.LEVERAGE,size_mult=size_mult)
        max_qty=(self.balance*cfg.LEVERAGE*0.50)/entry; qty=min(qty,max_qty)
        steps=int(qty/qty_step) if qty_step>0 else 0; qty=round(steps*qty_step,8)
        return max(qty,min_qty)
    def open_position_risk(self,pos_obj,sig,qty):
        pr=build_position_risk(sig.symbol,sig.direction,sig.entry,sig.sl,qty)
        self.positions[sig.symbol]=pr; return pr
    def close_position_risk(self,symbol): return self.positions.pop(symbol,None)
