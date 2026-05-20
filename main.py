import os, asyncio, uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager

from bot.engine import TradingEngine
from bot.bybit import BybitClient
from bot.logger import log
from bot import database as db


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("🚀 KAKAZITO TRADE iniciando...")
    client = BybitClient()
    engine = TradingEngine(client)
    app.state.client = client
    app.state.engine = engine
    asyncio.create_task(engine.run())
    log.info("✅ KAKAZITO TRADE online")
    yield
    engine.stop()
    await asyncio.sleep(0.5)
    await client.close()


app = FastAPI(title="KAKAZITO TRADE", version="10.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ── Health — SEMPRE 200 OK ───────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"status": "online", "version": "10.0.0", "name": "KAKAZITO TRADE"}


# ── Bot status ───────────────────────────────────────────────────
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
    return {"open": [p.to_dict() for p in eng.positions.values()],
            "count": len(eng.positions)}

@app.post("/api/pause")
async def pause():
    app.state.engine.stop()
    return {"message": "Bot pausado (servidor continua rodando)"}

@app.post("/api/resume")
async def resume():
    asyncio.create_task(app.state.engine.run())
    return {"message": "Bot retomado"}


# ── PnL / Stats ──────────────────────────────────────────────────
@app.get("/api/pnl")
async def pnl():
    return app.state.engine.stats.all_summaries()

@app.get("/api/db-stats")
async def db_stats():
    """Métricas persistidas: Sharpe, Sortino, Win Rate, Max DD, PF."""
    return await db.get_stats()

@app.get("/api/backtest/results")
async def backtest_results():
    """Resultados de backtest / performance histórica do banco."""
    import numpy as np
    stats   = await db.get_stats()
    trades  = stats.get("recent_trades", [])
    perf    = stats.get("performance", {})
    return {
        "summary": perf,
        "consecutive_losses": stats.get("consecutive_losses", 0),
        "bot_paused":         False,   # nunca pausa
        "today":              stats.get("today", {}),
        "recent_trades":      trades[:10],
    }


# ── Score pré-trade ──────────────────────────────────────────────
@app.get("/api/score/{symbol}")
async def score_endpoint(symbol: str, direction: str = "LONG"):
    """Calcula score pré-trade para um símbolo."""
    from bot import score as scoring
    try:
        kl = app.state.client.get_cached_klines(symbol.upper(), "15", 50)
        if len(kl) >= 10:
            c = [k["c"] for k in kl]
            h = [k["h"] for k in kl]
            l = [k["l"] for k in kl]
            v = [k["v"] for k in kl]
        else:
            kl = await app.state.client.get_klines(symbol.upper(), "15", 50)
            c = [k["c"] for k in kl]; h = [k["h"] for k in kl]
            l = [k["l"] for k in kl]; v = [k["v"] for k in kl]
        result = await scoring.calculate(
            symbol.upper(), direction.upper(),
            c, h, l, v, app.state.client,
        )
        return result
    except Exception as e:
        return {"error": str(e), "total": 0, "aprovado": False}


# ── Market data ──────────────────────────────────────────────────
@app.get("/api/market/{symbol}")
async def market(symbol: str):
    """Retorna OI, funding rate e ticker ao vivo."""
    sym = symbol.upper()
    try:
        oi  = await app.state.client.get_open_interest(sym)
        fr  = await app.state.client.get_funding_rate(sym)
        tk  = app.state.client.get_cached_ticker(sym)
        return {"symbol": sym, "open_interest": oi,
                "funding_rate": fr, "ticker": tk}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/news")
async def news():
    """Última notícia classificada pelo news reader."""
    from bot.score import _news_cache, _macro_cache
    return {
        "news":  _news_cache,
        "macro": _macro_cache,
    }

@app.get("/api/rankings")
async def rankings():
    from bot.engine import SYMBOLS
    return {"symbols": SYMBOLS[:10]}


# ── Dashboard ────────────────────────────────────────────────────
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    try:
        with open("dashboard/index.html") as f:
            return f.read()
    except Exception:
        return "<h1>Dashboard não encontrado</h1>"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
