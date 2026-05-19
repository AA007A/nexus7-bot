import os, asyncio, uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager

from bot.engine import TradingEngine
from bot.bybit import BybitClient
from bot.logger import log


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("🚀 KAKAZITO TRADE v7 iniciando...")
    client = BybitClient()
    engine = TradingEngine(client)
    app.state.client = client
    app.state.engine = engine

    # Engine roda em background — NÃO bloqueia o health check
    asyncio.create_task(engine.run())

    log.info("✅ KAKAZITO TRADE online — aguardando health check")
    yield
    # Shutdown
    engine.stop()
    await asyncio.sleep(0.5)
    await client.close()
    log.info("👋 KAKAZITO TRADE encerrado")


app = FastAPI(title="KAKAZITO TRADE", version="7.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Health check — responde IMEDIATAMENTE, sem depender do engine ──
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"status": "online", "version": "7.1.0", "name": "KAKAZITO TRADE"}

@app.get("/api/status")
async def status():
    return app.state.engine.get_status()

@app.get("/api/balance")
async def balance():
    b = await app.state.client.get_balance()
    return {"balance": b, "currency": "USDT"}

@app.get("/api/positions")
async def positions():
    eng = app.state.engine
    return {
        "open":  [p.to_dict() for p in eng.positions.values()],
        "count": len(eng.positions),
    }

@app.get("/api/pnl")
async def pnl():
    return app.state.engine.stats.all_summaries()

@app.get("/api/db-stats")
async def db_stats():
    """Métricas persistidas: Sharpe, Win Rate, Max DD, perdas consecutivas."""
    from bot import database as db
    return await db.get_stats_summary()

@app.get("/api/backtest")
async def backtest():
    """Mini backtesting com os últimos 30 trades fechados do banco."""
    from bot import database as db
    import numpy as np
    summary = await db.get_stats_summary()
    trades = summary.get("recent_trades", [])
    if not trades:
        return {"message": "Nenhum trade fechado ainda", "trades": 0}
    pnls = [t["pnl_net"] for t in trades]
    arr  = np.array(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    cum = np.cumsum(arr)
    peak = np.maximum.accumulate(cum)
    dd = (peak - cum)
    max_dd = float(dd.max()) if len(dd) else 0
    sharpe = float(arr.mean() / arr.std()) if arr.std() > 0 else 0
    avg_hold = sum(t["hold_min"] for t in trades) / len(trades) if trades else 0
    avg_win  = sum(wins)  / len(wins)   if wins   else 0
    avg_loss = sum(losses)/ len(losses) if losses else 0
    profit_factor = abs(sum(wins) / sum(losses)) if sum(losses) != 0 else 0
    return {
        "trades":         len(pnls),
        "win_rate":       round(len(wins)/len(pnls)*100, 1),
        "total_pnl_net":  round(float(arr.sum()), 4),
        "avg_win":        round(avg_win, 4),
        "avg_loss":       round(avg_loss, 4),
        "profit_factor":  round(profit_factor, 2),
        "max_drawdown":   round(max_dd, 4),
        "sharpe_ratio":   round(sharpe, 3),
        "avg_hold_min":   round(avg_hold, 1),
        "consecutive_losses": summary.get("consecutive_losses", 0),
        "recent_trades":  trades[:10],
    }

@app.post("/api/pause")
async def pause():
    app.state.engine.stop()
    return {"message": "Bot pausado"}

@app.post("/api/resume")
async def resume():
    asyncio.create_task(app.state.engine.run())
    return {"message": "Bot retomado"}

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    try:
        with open("dashboard/index.html") as f:
            return f.read()
    except Exception:
        return "<h1>Dashboard não encontrado</h1>"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
