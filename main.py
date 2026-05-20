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




# ── P3 — Backtest, CVD, Heatmap, Macro ───────────────────────────
@app.post("/api/backtest/run")
async def run_backtest(symbol: str = "BTCUSDT"):
    """Dispara backtest completo. Pode demorar 30-60s."""
    from bot.backtest import run_backtest as do_bt
    result = await do_bt(app.state.client, symbol.upper())
    return result

@app.get("/api/backtest/results")
async def backtest_results():
    """Último resultado de backtest do banco."""
    stats = await db.get_stats()
    return {
        "performance":        stats.get("performance", {}),
        "consecutive_losses": stats.get("consecutive_losses", 0),
        "bot_paused":         False,
        "today":              stats.get("today", {}),
        "recent_trades":      stats.get("recent_trades", [])[:10],
    }

@app.get("/api/cvd/{symbol}")
async def cvd(symbol: str):
    """CVD acumulado em tempo real via WebSocket."""
    from bot.market_data import get_cvd
    sym = symbol.upper()
    tk  = app.state.client.get_cached_ticker(sym)
    return {
        "symbol": sym,
        "cvd":    round(get_cvd(sym), 2),
        "price":  tk.get("lastPrice", 0),
        "bias":   "BULLISH" if get_cvd(sym) > 0 else "BEARISH",
    }

@app.get("/api/heatmap/{symbol}")
async def heatmap(symbol: str):
    """Clusters de liquidação estimados."""
    from bot.market_data import fetch_liq_heatmap
    return await fetch_liq_heatmap(app.state.client, symbol.upper())

@app.get("/api/macro")
async def macro():
    """Correlações macro: DXY, S&P500, BTC Dominance."""
    from bot.market_data import get_macro_summary
    from bot.score import _macro_cache
    return {
        "correlations": get_macro_summary(),
        "fear_greed":   _macro_cache.get("fear_greed", 50),
        "btc_dominance":_macro_cache.get("btc_dominance", 57.0),
    }

@app.get("/api/signals")
async def signals(limit: int = 20):
    """Últimos sinais gerados com scores detalhados."""
    rows = await db._fetchall(
        """SELECT symbol,direction,score_total,score_tecnico,
                  score_orderflow,score_macro,score_news,
                  entrou,motivo_rejeicao,timestamp
           FROM signals ORDER BY id DESC LIMIT ?""",
        (limit,)
    )
    return {
        "signals": [
            {
                "symbol":     r[0], "direction":   r[1],
                "score":      r[2], "tecnico":     r[3],
                "orderflow":  r[4], "macro":       r[5],
                "news_mod":   r[6], "entrou":      bool(r[7]),
                "motivo":     r[8], "timestamp":   r[9],
            }
            for r in (rows or [])
        ]
    }

@app.get("/api/risk-events")
async def risk_events(limit: int = 20):
    """Eventos de risco registrados."""
    rows = await db._fetchall(
        "SELECT tipo_evento,descricao,pnl_acumulado,timestamp FROM risk_events ORDER BY id DESC LIMIT ?",
        (limit,)
    )
    return {
        "events": [
            {"tipo": r[0], "descricao": r[1], "pnl": r[2], "timestamp": r[3]}
            for r in (rows or [])
        ]
    }

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
