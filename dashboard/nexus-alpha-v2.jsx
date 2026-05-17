import { useState, useEffect, useRef, useCallback } from "react";

// ── Preços reais 16/05/2026 (fallback se API bloquear)
const REAL_PRICES = {
  BTCUSDT:  { price: 78081.70, chg: -1.29, vol: "38.2B" },
  ETHUSDT:  { price: 2174.84,  chg: -2.12, vol: "12.1B" },
  BNBUSDT:  { price: 656.31,   chg: -1.44, vol: "1.7B"  },
  SOLUSDT:  { price: 86.18,    chg: -3.50, vol: "4.2B"  },
  ARBUSDT:  { price: 0.12,     chg: -2.80, vol: "84M"   },
  DOGEUSDT: { price: 0.1142,   chg: -2.10, vol: "890M"  },
  LINKUSDT: { price: 9.67,     chg: -3.49, vol: "320M"  },
  AVAXUSDT: { price: 19.82,    chg: -3.10, vol: "210M"  },
};

const C = {
  black:"#020408",bg1:"#060f1a",bg2:"#091520",dim2:"#0d2030",
  yellow:"#f5d800",green:"#00ff7f",cyan:"#00e5d4",
  mag:"#ff2d7a",orange:"#ff6b00",purple:"#9b59b6",
  text:"#7ab8c8",dim:"#1e3d50",
};
const mono = "'Share Tech Mono','Courier New',monospace";
const orb  = "'Courier New',monospace";

const fmt = (n,d=2) => parseFloat(n||0).toLocaleString("en-US",{minimumFractionDigits:d,maximumFractionDigits:d});

function Glow({ color, size=6 }) {
  return <span style={{ width:size,height:size,borderRadius:"50%",background:color,
    boxShadow:`0 0 ${size+2}px ${color}`,display:"inline-block",flexShrink:0 }}/>;
}

function Tag({ color=C.cyan, children }) {
  return <span style={{ fontFamily:mono,fontSize:8,letterSpacing:2,padding:"1px 7px",
    borderRadius:2,border:`1px solid ${color}44`,background:`${color}18`,color }}>{children}</span>;
}

function Panel({ children, accent=C.cyan, style={} }) {
  return <div style={{ background:"rgba(6,15,26,0.94)",border:`1px solid ${C.dim}`,
    borderRadius:3,position:"relative",overflow:"hidden",...style }}>
    <div style={{ position:"absolute",top:0,left:0,right:0,height:1,
      background:`linear-gradient(90deg,transparent,${accent},transparent)`,opacity:.6 }}/>
    {children}
  </div>;
}

function PH({ title, badge, bc=C.cyan, accent=C.cyan }) {
  return <div style={{ display:"flex",alignItems:"center",gap:8,padding:"6px 10px",
    borderBottom:`1px solid ${C.dim2}`,background:"rgba(0,0,0,0.35)" }}>
    <span style={{ fontFamily:orb,fontSize:8,letterSpacing:3,color:accent,textTransform:"uppercase" }}>{title}</span>
    {badge && <span style={{ marginLeft:"auto" }}><Tag color={bc}>{badge}</Tag></span>}
  </div>;
}

function MRow({ label, value, vc=C.text }) {
  return <div style={{ display:"flex",justifyContent:"space-between",alignItems:"center",
    padding:"5px 10px",borderBottom:`1px solid ${C.dim2}` }}>
    <span style={{ fontSize:9,letterSpacing:1,color:C.dim,textTransform:"uppercase" }}>{label}</span>
    <span style={{ fontFamily:mono,fontSize:11,color:vc }}>{value}</span>
  </div>;
}

// ── Neural net animada
function Neural() {
  const ref = useRef(null);
  useEffect(() => {
    const canvas = ref.current; if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const W = 560, H = 190; canvas.width=W; canvas.height=H;
    const cols=[C.mag,C.green,C.cyan,C.yellow,C.purple,C.orange];
    const lbls=["BEAR","BULL","MACRO","ZONE","CATALYST","PUMP","EMA","SUPPORT","RESIST","DUMP","SMC","VOID"];
    const N=26;
    const nodes=Array.from({length:N},(_,i)=>({
      x:60+Math.random()*(W-120), y:15+Math.random()*(H-30),
      r:4+Math.random()*9, color:cols[i%cols.length],
      label:lbls[Math.floor(Math.random()*lbls.length)],
      vx:(Math.random()-.5)*.35, vy:(Math.random()-.5)*.35,
      pulse:Math.random()*Math.PI*2,
    }));
    const edges=[];
    for(let i=0;i<N;i++) for(let j=i+1;j<N;j++){
      const dx=nodes[i].x-nodes[j].x, dy=nodes[i].y-nodes[j].y;
      if(Math.sqrt(dx*dx+dy*dy)<155 && Math.random()>.5) edges.push([i,j]);
    }
    let frame=0, raf;
    function draw(){
      ctx.clearRect(0,0,W,H); frame++;
      edges.forEach(([a,b])=>{
        const na=nodes[a],nb=nodes[b];
        ctx.beginPath(); ctx.moveTo(na.x,na.y); ctx.lineTo(nb.x,nb.y);
        ctx.strokeStyle=`rgba(0,229,212,${.04+.04*Math.sin(frame*.02+a)})`;
        ctx.lineWidth=.5; ctx.stroke();
      });
      nodes.forEach(n=>{
        n.x+=n.vx; n.y+=n.vy; n.pulse+=.04;
        if(n.x<n.r||n.x>W-n.r) n.vx*=-1;
        if(n.y<n.r||n.y>H-n.r) n.vy*=-1;
        const glow=n.r+3*Math.sin(n.pulse);
        const g=ctx.createRadialGradient(n.x,n.y,0,n.x,n.y,glow*2);
        g.addColorStop(0,n.color+"cc"); g.addColorStop(1,n.color+"00");
        ctx.beginPath(); ctx.arc(n.x,n.y,glow*2,0,Math.PI*2); ctx.fillStyle=g; ctx.fill();
        ctx.beginPath(); ctx.arc(n.x,n.y,n.r,0,Math.PI*2);
        ctx.fillStyle=n.color; ctx.shadowColor=n.color; ctx.shadowBlur=10; ctx.fill(); ctx.shadowBlur=0;
        if(n.r>7){ ctx.fillStyle="rgba(255,255,255,.55)"; ctx.font="7px monospace"; ctx.fillText(n.label,n.x+n.r+2,n.y+3); }
      });
      raf=requestAnimationFrame(draw);
    }
    draw();
    return ()=>cancelAnimationFrame(raf);
  },[]);
  return <canvas ref={ref} style={{ width:"100%",height:190,display:"block" }}/>;
}

// ── Equity curve
function Equity({ history }) {
  const ref=useRef(null);
  useEffect(()=>{
    const canvas=ref.current; if(!canvas||history.length<2) return;
    const ctx=canvas.getContext("2d"); const W=canvas.width,H=canvas.height;
    ctx.clearRect(0,0,W,H);
    const min=Math.min(...history),max=Math.max(...history),range=max-min||1;
    const pts=history.map((v,i)=>({x:i/(history.length-1)*W,y:H-(v-min)/range*(H-4)-2}));
    const isPos=history[history.length-1]>=0; const col=isPos?C.green:C.mag;
    const g=ctx.createLinearGradient(0,0,0,H);
    g.addColorStop(0,isPos?"rgba(0,255,127,.3)":"rgba(255,45,122,.3)"); g.addColorStop(1,"rgba(0,0,0,0)");
    ctx.beginPath(); ctx.moveTo(pts[0].x,H); pts.forEach(p=>ctx.lineTo(p.x,p.y));
    ctx.lineTo(pts[pts.length-1].x,H); ctx.fillStyle=g; ctx.fill();
    ctx.beginPath(); pts.forEach((p,i)=>i?ctx.lineTo(p.x,p.y):ctx.moveTo(p.x,p.y));
    ctx.strokeStyle=col; ctx.lineWidth=1.5; ctx.shadowColor=col; ctx.shadowBlur=6; ctx.stroke(); ctx.shadowBlur=0;
  },[history]);
  return <canvas ref={ref} width={260} height={50} style={{ width:"100%",height:50 }}/>;
}

// ── Bybit API fetch (real)
async function fetchBybit(sym) {
  const r = await fetch(`https://api.bybit.com/v5/market/tickers?category=linear&symbol=${sym}`);
  const d = await r.json();
  return d?.result?.list?.[0];
}

export default function A007ATrade() {
  const [time, setTime]       = useState(new Date());
  const [cycle, setCycle]     = useState(0);
  const [cycleN, setCycleN]   = useState(1);
  const [prices, setPrices]   = useState(REAL_PRICES);
  const [lastUpdate, setLastUpdate] = useState("aguardando...");
  const [apiOk, setApiOk]     = useState(false);
  const [botStatus, setBotStatus] = useState({
    active:true, balance:19.97, buying_power:99.85,
    drawdown_pct:2.4, open_positions:0, viable_symbols:19,
    win_rate:0, trades:0, pnl:0,
    daily_pnl:0, daily_target:100, daily_stop_loss:50,
    daily_target_hit:false, daily_stopped:false,
    daily_progress:0, mode:"AGRESSIVO", effective_score:80,
  });
  const [positions, setPositions] = useState([]);
  const [trades, setTrades]   = useState([]);
  const [equityHist] = useState(()=>{ const h=[0]; for(let i=1;i<30;i++) h.push(h[i-1]+(Math.random()-.42)*2); return h; });

  // Relógio
  useEffect(()=>{ const t=setInterval(()=>setTime(new Date()),1000); return ()=>clearInterval(t); },[]);

  // Cycle animation
  useEffect(()=>{
    const t=setInterval(()=>{
      setCycle(c=>{ const n=(c+1)%6; if(n===0) setCycleN(x=>x+1); return n; });
    },700);
    return ()=>clearInterval(t);
  },[]);

  // ── Fetch preços Bybit em tempo real
  const fetchPrices = useCallback(async () => {
    const syms = Object.keys(REAL_PRICES);
    let gotOne = false;
    const updated = { ...prices };
    for (const sym of syms) {
      try {
        const t = await fetchBybit(sym);
        if (!t) continue;
        updated[sym] = {
          price: parseFloat(t.lastPrice),
          chg:   parseFloat(t.price24hPcnt) * 100,
          vol:   updated[sym].vol,
          fr:    parseFloat(t.fundingRate || 0) * 100,
        };
        gotOne = true;
      } catch {}
      await new Promise(r => setTimeout(r, 120));
    }
    if (gotOne) {
      setPrices({ ...updated });
      setApiOk(true);
      setLastUpdate(new Date().toLocaleTimeString("pt-BR"));
    }
  }, []);

  // ── Fetch bot status
  const fetchBot = useCallback(async () => {
    try {
      const r = await fetch("/api/status");
      const s = await r.json();
      const sess = s.pnl_session || {};
      setBotStatus({
        active:          s.active ?? true,
        balance:         s.balance || 0,
        buying_power:    s.buying_power || 0,
        drawdown_pct:    parseFloat(s.drawdown_pct || 0),
        open_positions:  s.open_positions || (s.positions||[]).length,
        viable_symbols:  s.viable_symbols || 19,
        win_rate:        sess.win_rate || s.win_rate_pct || 0,
        trades:          sess.trades || (s.wins||0)+(s.losses||0),
        pnl:             typeof sess === "object" ? (sess.pnl||0) : sess,
        daily_pnl:       s.daily_pnl || 0,
        daily_target:    s.daily_target || 100,
        daily_stop_loss: s.daily_stop_loss || 50,
        daily_target_hit:s.daily_target_hit || false,
        daily_stopped:   s.daily_stopped || false,
        daily_progress:  s.daily_progress || 0,
        mode:            s.mode || "AGRESSIVO",
        effective_score: s.effective_score || 80,
      });
      setPositions(s.positions || []);
      setTrades((sess.closed_trades || s.closed_trades || []).slice(-8).reverse());
    } catch {}
  }, []);

  useEffect(() => {
    fetchPrices();
    fetchBot();
    const t1 = setInterval(fetchPrices, 12000);
    const t2 = setInterval(fetchBot,    8000);
    return () => { clearInterval(t1); clearInterval(t2); };
  }, [fetchPrices, fetchBot]);

  const btc = prices.BTCUSDT;
  const utc = `${String(time.getUTCHours()).padStart(2,"0")}:${String(time.getUTCMinutes()).padStart(2,"0")}:${String(time.getUTCSeconds()).padStart(2,"0")} UTC`;
  const steps = ["SCAN","DETECT","VALIDATE","EXECUTE","MONITOR","EXIT"];

  return (
    <div style={{ background:C.black,minHeight:"100vh",fontFamily:mono,fontSize:12,color:C.text,overflow:"hidden" }}>

      {/* ── TOP BAR ── */}
      <div style={{ height:40,background:C.bg1,borderBottom:`1px solid ${C.dim}`,
        display:"flex",alignItems:"center",padding:"0 12px",gap:0,
        position:"sticky",top:0,zIndex:100 }}>

        <div style={{ fontFamily:orb,fontSize:14,fontWeight:900,letterSpacing:4,
          color:C.yellow,textShadow:`0 0 20px ${C.yellow}`,
          borderRight:`1px solid ${C.dim}`,paddingRight:14,marginRight:14 }}>
          A007A<span style={{ color:C.cyan }}>TRADE</span>
        </div>

        {/* SYNC badge */}
        <div style={{ display:"flex",alignItems:"center",gap:6,fontFamily:orb,fontSize:8,
          letterSpacing:3,color:C.green,border:`1px solid ${C.green}44`,borderRadius:2,
          padding:"2px 8px",background:`${C.green}12`,marginRight:14 }}>
          <Glow color={C.green} size={6}/>
          {apiOk ? `BYBIT SYNC · ${lastUpdate}` : "BOT SYNC ACTIVE"}
        </div>

        {/* Tickers ao vivo */}
        {[["BTC","BTCUSDT",2],["ETH","ETHUSDT",2],["BNB","BNBUSDT",2],["SOL","SOLUSDT",2],["ARB","ARBUSDT",4]].map(([sym,key,dec])=>{
          const d = prices[key]; if(!d) return null;
          const up = d.chg >= 0;
          return <div key={sym} style={{ display:"flex",alignItems:"center",gap:5,
            padding:"0 11px",borderRight:`1px solid ${C.dim}`,height:40 }}>
            <span style={{ fontFamily:orb,fontSize:9,letterSpacing:2,color:C.dim }}>{sym}</span>
            <span style={{ fontFamily:mono,fontSize:13,color:C.text }}>${fmt(d.price,dec)}</span>
            <span style={{ fontSize:10,color:up?C.green:C.mag,fontWeight:"bold" }}>{up?"+":""}{d.chg.toFixed(2)}%</span>
          </div>;
        })}

        <div style={{ marginLeft:"auto",display:"flex",gap:0,alignItems:"center" }}>
          <div style={{ textAlign:"center",padding:"0 12px",borderLeft:`1px solid ${C.dim}` }}>
            <div style={{ fontSize:7,letterSpacing:2,color:C.dim }}>VOL 24H BTC</div>
            <div style={{ fontFamily:mono,fontSize:11 }}>$38.2B</div>
          </div>
          <div style={{ textAlign:"center",padding:"0 12px",borderLeft:`1px solid ${C.dim}` }}>
            <div style={{ fontSize:7,letterSpacing:2,color:C.dim }}>FUNDING</div>
            <div style={{ fontFamily:mono,fontSize:11,color:btc?.fr>=0?C.green:C.mag }}>
              {btc?.fr !== undefined ? `${btc.fr>=0?"+":""}${btc.fr.toFixed(4)}%` : "+0.0082%"}
            </div>
          </div>
          <div style={{ fontFamily:orb,fontSize:12,color:C.cyan,letterSpacing:2,
            padding:"0 12px",borderLeft:`1px solid ${C.dim}` }}>{utc}</div>
        </div>
      </div>

      {/* ── EXEC CYCLE ── */}
      <div style={{ background:C.bg1,borderBottom:`1px solid ${C.yellow}22`,
        padding:"3px 12px",display:"flex",alignItems:"center",gap:4 }}>
        <span style={{ fontFamily:orb,fontSize:8,letterSpacing:3,color:C.yellow,marginRight:10 }}>EXEC CYCLE</span>
        {steps.map((s,i)=>(
          <div key={i} style={{ display:"flex",alignItems:"center",gap:2 }}>
            <div style={{ display:"flex",flexDirection:"column",alignItems:"center",
              padding:"1px 9px",borderRight:`1px solid ${C.dim}` }}>
              <span style={{ fontSize:7,color:C.dim }}>{String(i+1).padStart(2,"0")}</span>
              <span style={{ fontFamily:orb,fontSize:9,letterSpacing:2,
                color:cycle===i?C.yellow:C.cyan,
                textShadow:cycle===i?`0 0 8px ${C.yellow}`:"none" }}>{s}</span>
            </div>
            {i<5 && <span style={{ color:C.dim,fontSize:11 }}>›</span>}
          </div>
        ))}
        <div style={{ marginLeft:"auto",display:"flex",gap:10,alignItems:"center",fontFamily:mono,fontSize:10 }}>
          <span style={{ color:C.dim }}>CYCLE <span style={{ color:C.yellow }}>#{cycleN}</span></span>
          <Tag color={C.green}>UNDER BUDGET</Tag>
        </div>
      </div>

      {/* ── MAIN GRID ── */}
      <div style={{ display:"grid",gridTemplateColumns:"255px 1fr 275px",gap:6,padding:6 }}>

        {/* LEFT */}
        <div style={{ display:"flex",flexDirection:"column",gap:6 }}>

          {/* Wallet */}
          <div style={{ background:`linear-gradient(135deg,${C.yellow}0a,transparent 60%)`,
            border:`1px solid ${C.yellow}28`,borderRadius:3,padding:12 }}>
            <div style={{ fontSize:9,color:C.dim,marginBottom:4 }}>A007A TRADE · BYBIT FUTURES · ENGINE v7.0</div>
            <div style={{ display:"inline-block",background:`${C.green}18`,border:`1px solid ${C.green}44`,
              borderRadius:2,padding:"1px 7px",fontFamily:orb,fontSize:8,letterSpacing:2,
              color:C.green,marginBottom:8 }}>● LIVE</div>
            <div style={{ fontFamily:orb,fontSize:26,fontWeight:900,
              color:botStatus.pnl>=0?C.green:C.mag,
              textShadow:`0 0 20px ${botStatus.pnl>=0?C.green:C.mag}66`,lineHeight:1 }}>
              {botStatus.pnl>=0?"+":"-"}${fmt(Math.abs(botStatus.pnl))}
            </div>
            <div style={{ fontFamily:mono,fontSize:9,color:C.green,opacity:.7,marginTop:2 }}>SESSION P&L</div>
            <div style={{ display:"grid",gridTemplateColumns:"1fr 1fr 1fr",gap:8,
              marginTop:10,borderTop:`1px solid ${C.dim2}`,paddingTop:8 }}>
              {[["BALANCE","$"+fmt(botStatus.balance)],["WIN RATE",botStatus.win_rate+"%"],["TRADES",botStatus.trades||"—"]].map(([l,v])=>(
                <div key={l} style={{ textAlign:"center" }}>
                  <div style={{ fontSize:7,letterSpacing:1,color:C.dim }}>{l}</div>
                  <div style={{ fontFamily:mono,fontSize:12,color:C.text }}>{v}</div>
                </div>
              ))}
            </div>
          </div>

          {/* Bot Metrics */}
          <Panel accent={C.yellow}>
            <PH title="BOT METRICS" badge={botStatus.active?"ACTIVE":"PAUSED"} bc={botStatus.active?C.green:C.yellow} accent={C.yellow}/>
            <MRow label="Open Positions" value={`${botStatus.open_positions} / 3`} vc={botStatus.open_positions>=3?C.mag:C.yellow}/>
            <MRow label="Min Entry Score" value="80 / 100" vc={C.yellow}/>
            <MRow label="Leverage" value="5×"/>
            <MRow label="Drawdown" value={botStatus.drawdown_pct.toFixed(2)+"%"} vc={botStatus.drawdown_pct>10?C.mag:C.green}/>
            <MRow label="Buying Power" value={"$"+fmt(botStatus.buying_power)} vc={C.cyan}/>
            <MRow label="Viable Pairs" value={botStatus.viable_symbols}/>
          </Panel>

          {/* AI Score */}
          <Panel>
            <PH title="AI CONFIDENCE"/>
            <div style={{ padding:10 }}>
              <div style={{ fontFamily:orb,fontSize:8,letterSpacing:2,color:C.cyan,marginBottom:4 }}>NEURAL ENGINE SCORE</div>
              <div style={{ fontFamily:orb,fontSize:24,fontWeight:900,color:C.cyan,textShadow:`0 0 15px ${C.cyan}88` }}>80/100</div>
              <div style={{ height:4,background:C.dim2,borderRadius:2,overflow:"hidden",margin:"6px 0 4px" }}>
                <div style={{ height:"100%",width:"80%",background:`linear-gradient(90deg,${C.cyan},${C.green})`,boxShadow:`0 0 8px ${C.cyan}` }}/>
              </div>
              <div style={{ display:"flex",justifyContent:"space-between" }}>
                <span style={{ fontSize:8,color:C.dim }}>REGIME: <span style={{ color:C.yellow }}>TRENDING ↓</span></span>
                <span style={{ fontSize:8,color:C.dim }}>SIGNAL: <span style={{ color:C.cyan }}>{positions.length?"ACTIVE":"SCANNING"}</span></span>
              </div>
            </div>
          </Panel>

          {/* Exchanges */}
          <Panel>
            <PH title="EXCHANGES"/>
            <div style={{ display:"grid",gridTemplateColumns:"1fr 1fr",gap:4,padding:8 }}>
              {[["BYBIT",true],["BINANCE",false],["OKX",false],["HYPERLIQUID",false]].map(([name,on])=>(
                <div key={name} style={{ border:`1px solid ${on?C.green+"33":C.dim2}`,borderRadius:2,
                  padding:"5px 8px",display:"flex",alignItems:"center",gap:5,
                  background:on?`${C.green}08`:"transparent" }}>
                  <div style={{ width:5,height:5,borderRadius:"50%",background:on?C.green:C.dim,
                    boxShadow:on?`0 0 6px ${C.green}`:"none" }}/>
                  <span style={{ fontFamily:orb,fontSize:8,letterSpacing:1,color:on?C.green:C.dim }}>{name}</span>
                </div>
              ))}
            </div>
          </Panel>

          {/* Meta Diária */}
          <Panel accent={botStatus.daily_target_hit ? C.green : botStatus.daily_stopped ? C.mag : C.yellow}>
            <PH
              title="META DIÁRIA"
              badge={botStatus.daily_target_hit ? "✅ BATIDA" : botStatus.daily_stopped ? "🛑 PARADO" : `${botStatus.mode}`}
              bc={botStatus.daily_target_hit ? C.green : botStatus.daily_stopped ? C.mag : C.yellow}
              accent={botStatus.daily_target_hit ? C.green : botStatus.daily_stopped ? C.mag : C.yellow}
            />
            <div style={{ padding:"10px 12px" }}>
              {/* Valor atual vs meta */}
              <div style={{ display:"flex", justifyContent:"space-between", alignItems:"flex-end", marginBottom:6 }}>
                <div>
                  <div style={{ fontSize:7, letterSpacing:2, color:C.dim, marginBottom:2 }}>LUCRO HOJE</div>
                  <div style={{ fontFamily:orb, fontSize:22, fontWeight:900,
                    color: botStatus.daily_pnl >= botStatus.daily_target ? C.green :
                           botStatus.daily_pnl < 0 ? C.mag : C.yellow,
                    textShadow:`0 0 15px ${botStatus.daily_pnl >= 0 ? C.yellow : C.mag}66` }}>
                    {botStatus.daily_pnl >= 0 ? "+" : ""}${fmt(botStatus.daily_pnl)}
                  </div>
                </div>
                <div style={{ textAlign:"right" }}>
                  <div style={{ fontSize:7, letterSpacing:2, color:C.dim, marginBottom:2 }}>META</div>
                  <div style={{ fontFamily:mono, fontSize:16, color:C.yellow }}>
                    ${fmt(botStatus.daily_target)}
                  </div>
                </div>
              </div>

              {/* Barra de progresso */}
              <div style={{ height:6, background:C.dim2, borderRadius:3, overflow:"hidden", marginBottom:4 }}>
                <div style={{
                  height:"100%",
                  width: botStatus.daily_pnl < 0
                    ? "0%"
                    : Math.min(botStatus.daily_progress, 100) + "%",
                  background: botStatus.daily_target_hit
                    ? `linear-gradient(90deg,${C.green},${C.cyan})`
                    : `linear-gradient(90deg,${C.yellow},${C.green})`,
                  boxShadow: `0 0 8px ${botStatus.daily_target_hit ? C.green : C.yellow}`,
                  borderRadius:3, transition:"width 1s",
                }} />
              </div>

              <div style={{ display:"flex", justifyContent:"space-between", fontSize:9, marginBottom:8 }}>
                <span style={{ color:C.dim }}>
                  {botStatus.daily_progress.toFixed(1)}% da meta
                </span>
                <span style={{ color:C.dim }}>
                  Faltam: <span style={{ color:C.yellow }}>
                    ${fmt(Math.max(0, botStatus.daily_target - botStatus.daily_pnl))}
                  </span>
                </span>
              </div>

              {/* Linha stop loss */}
              <div style={{ display:"flex", justifyContent:"space-between",
                padding:"4px 8px", background:`${C.mag}0a`,
                border:`1px solid ${C.mag}22`, borderRadius:2, marginBottom:6 }}>
                <span style={{ fontSize:9, color:C.dim }}>STOP-LOSS DIÁRIO</span>
                <span style={{ fontFamily:mono, fontSize:10, color:C.mag }}>
                  -${fmt(botStatus.daily_stop_loss)}
                </span>
              </div>

              {/* Modo atual */}
              <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center" }}>
                <span style={{ fontSize:8, letterSpacing:1, color:C.dim }}>MODO</span>
                <Tag color={botStatus.daily_target_hit ? C.cyan : botStatus.daily_stopped ? C.mag : C.yellow}>
                  {botStatus.daily_target_hit
                    ? `CONSERVADOR · score ≥ ${botStatus.effective_score}`
                    : botStatus.daily_stopped
                    ? "PARADO · aguarda meia-noite"
                    : `AGRESSIVO · score ≥ ${botStatus.effective_score}`}
                </Tag>
              </div>
            </div>
          </Panel>

          <Panel style={{ flex:1 }}>
            <PH title="EQUITY CURVE" badge={(botStatus.pnl>=0?"+":"")+"$"+fmt(botStatus.pnl)} bc={botStatus.pnl>=0?C.green:C.mag}/>
            <div style={{ padding:"8px 10px" }}><Equity history={equityHist}/></div>
          </Panel>
        </div>

        {/* CENTER */}
        <div style={{ display:"flex",flexDirection:"column",gap:6 }}>
          <Panel style={{ flex:1,display:"flex",flexDirection:"column" }}>
            <PH title="FORCE / MOVEMENT GRAPH" badge="BTC/USDT · 15m · BYBIT"/>
            <div style={{ display:"flex",alignItems:"center",gap:10,padding:"7px 12px",
              borderBottom:`1px solid ${C.dim2}`,background:"rgba(0,0,0,0.2)",flexWrap:"wrap" }}>
              <span style={{ fontFamily:orb,fontSize:22,fontWeight:900,color:C.cyan,textShadow:`0 0 15px ${C.cyan}66` }}>
                ${fmt(btc?.price||78081)}
              </span>
              <span style={{ fontFamily:mono,fontSize:11,padding:"2px 7px",borderRadius:2,
                background:`${btc?.chg>=0?C.green:C.mag}12`,border:`1px solid ${btc?.chg>=0?C.green:C.mag}44`,
                color:btc?.chg>=0?C.green:C.mag }}>
                {btc?.chg>=0?"▲":"▼"} {btc?.chg>=0?"+":""}{(btc?.chg||0).toFixed(2)}%
              </span>
              {["BTC","ETH","SOL","BNB"].map(s=>(
                <button key={s} style={{ fontFamily:orb,fontSize:8,letterSpacing:2,padding:"3px 8px",
                  borderRadius:2,border:`1px solid ${C.cyan}55`,color:C.cyan,
                  background:s==="BTC"?`${C.cyan}20`:"transparent",cursor:"pointer" }}>{s}</button>
              ))}
            </div>
            <div style={{ flex:1,minHeight:340 }}>
              <iframe
                src="https://s.tradingview.com/widgetembed/?frameElementId=tv&symbol=BINANCE%3ABTCUSDT&interval=15&hidesidetoolbar=0&hidetoptoolbar=0&symboledit=1&saveimage=0&toolbarbg=060f1a&studies=MASimple%40tv-basicstudies%1FBB%40tv-basicstudies%1FRSI%40tv-basicstudies%1FMACD%40tv-basicstudies&theme=dark&style=1&timezone=Etc%2FUTC&withdateranges=1&locale=en"
                style={{ width:"100%",height:"100%",border:"none",minHeight:340 }}
                allowFullScreen title="TradingView BTC"/>
            </div>
          </Panel>

          {/* Analytics */}
          <div style={{ display:"grid",gridTemplateColumns:"repeat(4,1fr)",gap:6 }}>
            {[
              { t:"VOLATILITY BTC", v:Math.abs(btc?.chg||1.29).toFixed(2)+"%", s:"ATR 14 · 15min", c:C.yellow, w:Math.min(Math.abs(btc?.chg||1)*15,100)+"%" },
              { t:"WHALE ALERT",    v:"$12M SHORT",  s:"Detectado 14:22 UTC", c:C.cyan,   w:"60%" },
              { t:"FEAR & GREED",   v:"FEAR · 38",   s:"Market sentiment",    c:C.mag,    w:"38%" },
              { t:"SMART MONEY",    v:"BEAR 63%",     s:"Inst. flow bias",     c:C.green,  w:"37%" },
            ].map(a=>(
              <div key={a.t} style={{ background:"rgba(6,15,26,0.9)",border:`1px solid ${C.dim2}`,borderRadius:3,padding:8 }}>
                <div style={{ fontFamily:orb,fontSize:7,letterSpacing:2,color:C.dim,marginBottom:4 }}>{a.t}</div>
                <div style={{ fontFamily:mono,fontSize:13,color:a.c }}>{a.v}</div>
                <div style={{ fontSize:9,color:C.dim,marginTop:2 }}>{a.s}</div>
                <div style={{ height:3,background:C.dim2,borderRadius:2,marginTop:4,overflow:"hidden" }}>
                  <div style={{ height:"100%",width:a.w,background:a.c,borderRadius:2 }}/>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* RIGHT — ORDERS */}
        <div style={{ display:"flex",flexDirection:"column",gap:6 }}>
          <Panel style={{ flex:1,display:"flex",flexDirection:"column" }} accent={C.green}>
            <PH title="LIVE ORDERS · BOT" badge={`${positions.length} OPEN`} bc={positions.length?C.green:C.dim} accent={C.green}/>
            <div style={{ overflowY:"auto",flex:1,maxHeight:420 }}>
              {!positions.length ? (
                <div style={{ padding:20,textAlign:"center",color:C.dim }}>
                  <div style={{ fontSize:20,marginBottom:6 }}>⬡</div>
                  <div style={{ fontFamily:orb,fontSize:9,letterSpacing:2 }}>AGUARDANDO SINAL</div>
                  <div style={{ fontSize:10,marginTop:4 }}>Score mín. 80/100 para entrar</div>
                </div>
              ) : positions.map((p,i)=>{
                const sym=(p.symbol||"").replace("USDT","");
                const dir=p.direction||(p.side==="Buy"?"LONG":"SHORT");
                const isL=dir==="LONG"; const col=isL?C.green:C.mag;
                const pc=parseFloat(p.pnl_pct||0), pnl=parseFloat(p.pnl||0);
                const entry=parseFloat(p.entry||p.entry_price||0);
                const cur=parseFloat(p.current_price||entry);
                const sl=parseFloat(p.trailing_sl||p.sl||p.sl_price||0);
                const tp=parseFloat(p.tp||p.tp_price||0);
                const liq=isL?entry*.82:entry*1.18;
                return (
                  <div key={i} style={{ borderBottom:`1px solid ${C.dim2}`,padding:"8px 10px" }}>
                    <div style={{ display:"flex",alignItems:"center",gap:6,marginBottom:6 }}>
                      <span style={{ fontFamily:orb,fontSize:11,fontWeight:700,color:C.cyan }}>{sym}/USDT</span>
                      <span style={{ background:`${col}18`,border:`1px solid ${col}44`,color:col,
                        fontFamily:orb,fontSize:7,letterSpacing:2,padding:"1px 6px",borderRadius:2 }}>{dir}</span>
                      <span style={{ marginLeft:"auto",fontFamily:mono,fontSize:9,color:C.yellow }}>⬡ {p.score||75}/100</span>
                    </div>
                    <div style={{ display:"grid",gridTemplateColumns:"1fr 1fr",gap:"3px 8px" }}>
                      {[["ENTRY","$"+fmt(entry,4),C.text],["CURRENT","$"+fmt(cur,4),C.cyan],
                        ["STOP LOSS","$"+fmt(sl,4),C.yellow],["TAKE PROFIT","$"+fmt(tp,4),C.green],
                        ["LIQ PRICE","$"+fmt(liq,2),C.mag],["LEVERAGE","5×",C.text],
                        ["UNREAL PNL",(pnl>=0?"+":"")+"$"+fmt(pnl,4),col],
                        ["PnL %",(pc>=0?"+":"")+pc.toFixed(2)+"%",col],
                        ["ORDER TYPE","MARKET",C.dim],["LATENCY",Math.floor(Math.random()*50+10)+"ms",C.cyan],
                      ].map(([l,v,c])=>(
                        <div key={l}>
                          <div style={{ fontSize:7,letterSpacing:1,color:C.dim,textTransform:"uppercase" }}>{l}</div>
                          <div style={{ fontFamily:mono,fontSize:10,color:c }}>{v}</div>
                        </div>
                      ))}
                    </div>
                    <div style={{ height:2,background:C.dim2,borderRadius:1,marginTop:5,overflow:"hidden" }}>
                      <div style={{ height:"100%",width:Math.min(Math.abs(pc)*2,100)+"%",background:col }}/>
                    </div>
                    {p.trailing_active && (
                      <div style={{ fontSize:7,letterSpacing:1,color:C.yellow,border:`1px solid ${C.yellow}44`,
                        borderRadius:2,padding:"1px 5px",display:"inline-block",marginTop:4 }}>
                        🔄 TRAILING SL @ ${fmt(sl,4)}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </Panel>

          {/* Kill switch */}
          <div style={{ border:`1px solid ${C.mag}55`,background:`${C.mag}0a`,borderRadius:3,
            padding:"8px 12px",display:"flex",alignItems:"center",gap:8,cursor:"pointer" }}>
            <span style={{ fontSize:16 }}>⚡</span>
            <div>
              <div style={{ fontFamily:orb,fontSize:8,letterSpacing:2,color:C.mag }}>RISK KILL-SWITCH</div>
              <div style={{ fontSize:9,color:C.dim }}>{botStatus.active?"Bot ativo — clique para pausar":"Bot pausado"}</div>
            </div>
          </div>

          {/* Live analytics */}
          <Panel accent={C.mag}>
            <PH title="LIVE ANALYTICS" accent={C.mag}/>
            <div style={{ display:"grid",gridTemplateColumns:"1fr 1fr" }}>
              {[
                ["FUNDING",    btc?.fr!==undefined?`${btc.fr>=0?"+":""}${btc.fr.toFixed(4)}% /8H`:"+0.0082% /8H", C.green],
                ["OI DELTA",   "+$142M", C.cyan],
                ["LIQ MAP",    "$3.1M LONG",  C.yellow],
                ["ARBI OPP",   "BYB/BIN +0.3%",C.green],
                ["MONTE CARLO","93.1% BEAR",  C.mag],
                ["DOM HEATMAP","ASK HEAVY",   C.text],
                ["LIQ SWEEP",  "CLEAR",       C.green],
                ["GAMMA EXP",  "-1.42%",      C.mag],
              ].map(([l,v,c])=>(
                <div key={l} style={{ padding:"6px 10px",borderBottom:`1px solid ${C.dim2}`,borderRight:`1px solid ${C.dim2}` }}>
                  <div style={{ fontSize:7,letterSpacing:1,color:C.dim,textTransform:"uppercase" }}>{l}</div>
                  <div style={{ fontFamily:mono,fontSize:11,color:c,marginTop:1 }}>{v}</div>
                </div>
              ))}
            </div>
          </Panel>
        </div>
      </div>

      {/* ── NEURAL + TRADES + INTEL ── */}
      <div style={{ display:"grid",gridTemplateColumns:"1fr 235px 235px",gap:6,padding:"0 6px 6px" }}>

        <Panel accent={C.yellow}>
          <PH title="A007A TRADE · FORCE GRAPH" badge="LIVE AI NETWORK" bc={C.yellow} accent={C.yellow}/>
          <div style={{ position:"relative" }}>
            <Neural/>
            <div style={{ position:"absolute",top:8,left:8,display:"flex",flexDirection:"column",gap:4 }}>
              {[[C.mag,"BEAR SIGNAL"],[C.green,"BULL SIGNAL"],[C.cyan,"MEDIAN PATH"],[C.yellow,"CATALYST"],[C.purple,"CLUSTER"]].map(([c,l])=>(
                <div key={l} style={{ display:"flex",alignItems:"center",gap:5,fontFamily:mono,fontSize:9 }}>
                  <Glow color={c}/><span style={{ color:c }}>{l}</span>
                </div>
              ))}
            </div>
            <div style={{ position:"absolute",top:8,right:8,textAlign:"right",fontFamily:mono,fontSize:9 }}>
              <div><span style={{ color:C.dim }}>CONVERGENCE </span><span style={{ color:C.green }}>94.8%</span></div>
              <div><span style={{ color:C.dim }}>BEAR PATHS  </span><span style={{ color:C.mag }}>1,427</span></div>
              <div><span style={{ color:C.dim }}>BULL PATHS  </span><span style={{ color:C.green }}>639</span></div>
              <div><span style={{ color:C.dim }}>SIGNAL      </span><span style={{ color:C.mag }}>STRONG DOWN</span></div>
            </div>
          </div>
        </Panel>

        {/* Recent trades */}
        <Panel>
          <PH title="RECENT TRADES" badge={(botStatus.trades||"—")+" / 30D"}/>
          {!trades.length ? (
            <div style={{ padding:12,textAlign:"center",color:C.dim,fontSize:10 }}>Nenhum trade fechado</div>
          ) : trades.map((t,i)=>{
            const pct=parseFloat(t.pnl_pct||0);
            const col=pct>=0?C.green:C.mag;
            return (
              <div key={i} style={{ display:"flex",alignItems:"center",gap:6,padding:"5px 10px",
                borderBottom:`1px solid ${C.dim2}`,fontFamily:mono,fontSize:10 }}>
                <span style={{ color:C.dim,minWidth:24 }}>#{i+1}</span>
                <span style={{ color:col }}>{pct>=0?"▲ UP":"▼ DN"}</span>
                <span style={{ color:C.cyan }}>{(t.symbol||"").replace("USDT","")}</span>
                <span style={{ flex:1,textAlign:"right",color:col }}>{pct>=0?"+":""}{pct.toFixed(2)}%</span>
                <span style={{ color:C.dim,fontSize:9 }}>${fmt(t.pnl_usdt||t.pnl||0,2)}</span>
              </div>
            );
          })}
        </Panel>

        {/* Market Intel */}
        <Panel accent={C.mag}>
          <PH title="MARKET INTELLIGENCE" accent={C.mag}/>
          <div style={{ display:"grid",gridTemplateColumns:"1fr 1fr" }}>
            {[
              ["LIQ SWEEP",    "CLEAR",         C.green],
              ["MARKET REGIME","TRENDING ↓",    C.mag],
              ["AI ANOMALY",   "NORMAL",         C.green],
              ["SMART ROUTING","BYBIT",          C.green],
              ["LATENCY ARB",  "1.3ms WIN",      C.cyan],
              ["SYNTH SPREAD", "+0.0042",        C.cyan],
              ["FOOTPRINT",    "SELL DOM",       C.mag],
              ["RISK SCORE",   "28/100",         C.green],
            ].map(([l,v,c])=>(
              <div key={l} style={{ padding:"6px 10px",borderBottom:`1px solid ${C.dim2}`,borderRight:`1px solid ${C.dim2}` }}>
                <div style={{ fontSize:7,letterSpacing:1,color:C.dim,textTransform:"uppercase" }}>{l}</div>
                <div style={{ fontFamily:mono,fontSize:10,color:c,marginTop:1 }}>{v}</div>
              </div>
            ))}
          </div>
        </Panel>
      </div>
    </div>
  );
}
