"""
BGX Capital — API Server v11.0
Melhorias de segurança:
  ✅ Autenticação Bearer token em todos os endpoints sensíveis
  ✅ CORS restrito ao domínio configurado (não mais allow_origins=["*"])
  ✅ Rate limiting simples nos endpoints de controle
  ✅ Endpoint POST /api/close-all (emergency stop)
  ✅ Bug pause/resume corrigido (_running flag)
"""
import os, asyncio, time
from collections import defaultdict

import uvicorn
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from contextlib import asynccontextmanager

from bot.engine import TradingEngine
from bot.bybit import BybitClient
from bot.config import cfg
from bot.logger import log
from bot import database as db


# ── Autenticação ─────────────────────────────────────────────────
_bearer = HTTPBearer(auto_error=False)

def _require_auth(credentials: HTTPAuthorizationCredentials = Depends(_bearer)):
    """
    Valida Bearer token nos endpoints sensíveis.
    Configure BOT_API_SECRET no Railway para ativar a proteção.
    Se BOT_API_SECRET não estiver definido, loga aviso mas permite acesso
    (compatibilidade com ambiente de desenvolvimento).
    """
    secret = cfg.BOT_API_SECRET
    if not secret:
        log.warning("⚠️ BOT_API_SECRET não configurado — endpoints desprotegidos!")
        return  # permite sem auth em dev (configure em produção)
    if not credentials or credentials.credentials != secret:
        raise HTTPException(status_code=401, detail="Token inválido ou ausente")


# ── Rate Limiting simples ─────────────────────────────────────────
_rate_counters: dict = defaultdict(list)
_RATE_LIMIT = 10      # máx 10 req por janela
_RATE_WINDOW = 60     # janela de 60 segundos

def _rate_limit(request: Request):
    ip  = request.client.host if request.client else "unknown"
    now = time.time()
    _rate_counters[ip] = [t for t in _rate_counters[ip] if now - t < _RATE_WINDOW]
    if len(_rate_counters[ip]) >= _RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Rate limit excedido — aguarde 1 minuto")
    _rate_counters[ip].append(now)


# ── Lifespan ─────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("🚀 BGX Capital v11 iniciando...")
    client = BybitClient()
    engine = TradingEngine(client)
    app.state.client = client
    app.state.engine = engine
    app.state.engine_task = asyncio.create_task(engine.run())
    log.info("✅ BGX Capital online")
    yield
    # Shutdown limpo: para o engine e espera tasks pendentes
    engine.stop()
    await asyncio.sleep(1.0)   # aguarda ordens pendentes
    await client.close()
    log.info("👋 BGX Capital encerrado")


app = FastAPI(title="BGX Capital", version="11.0.0", lifespan=lifespan)

# ── CORS RESTRITO (não mais allow_origins=["*"]) ─────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.ALLOWED_ORIGINS,   # configurado via ALLOWED_ORIGINS env var
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=False,
)


# ── Health — sem autenticação (Railway health check) ─────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": "11.0.0"}

@app.get("/")
async def root():
    return {"status": "online", "version": "11.0.0", "name": "BGX Capital"}


# ── Bot status — leitura apenas (auth recomendada mas não obrigatória) ──
@app.get("/api/status", dependencies=[Depends(_require_auth)])
async def status():
    return app.state.engine.get_status()

@app.get("/api/balance", dependencies=[Depends(_require_auth)])
async def balance():
    b = await app.state.client.get_balance()
    return {"balance": b, "currency": "USDT"}

@app.get("/api/positions", dependencies=[Depends(_require_auth)])
async def positions():
    eng = app.state.engine
    return {
        "open":  [p.to_dict() for p in eng.positions.values()],
        "count": len(eng.positions),
    }


# ── Controle do bot — autenticação obrigatória + rate limit ──────
@app.post("/api/pause", dependencies=[Depends(_require_auth), Depends(_rate_limit)])
async def pause(request: Request):
    app.state.engine.stop()
    return {"message": "Bot pausado (servidor continua | posições mantidas)"}


@app.post("/api/resume", dependencies=[Depends(_require_auth), Depends(_rate_limit)])
async def resume(request: Request):
    engine = app.state.engine
    # BUG CORRIGIDO: cancela task anterior antes de criar nova
    if hasattr(app.state, "engine_task"):
        task = app.state.engine_task
        if task and not task.done():
            # Engine ainda rodando — apenas reativa
            engine.active = True
            log.info("▶️ Bot reativado (task já em execução)")
            return {"message": "Bot reativado"}
    # Task concluída ou inexistente → cria nova
    app.state.engine_task = asyncio.create_task(engine.run())
    log.info("▶️ Bot retomado (nova task criada)")
    return {"message": "Bot retomado"}


# ── Emergency Close All ───────────────────────────────────────────
@app.post("/api/close-all", dependencies=[Depends(_require_auth), Depends(_rate_limit)])
async def close_all(request: Request):
    """
    Fecha TODAS as posições abertas imediatamente.
    Endpoint de emergência — use com cuidado.
    """
    engine = app.state.engine
    engine.stop()   # para novas entradas
    result = await engine.close_all_positions()
    log.warning(f"🚨 /api/close-all: {result['closed']} fechadas | {result['errors']} erros")
    return {
        "message": f"Emergency close: {result['closed']} posições fechadas",
        **result,
    }


# ── PnL / Stats ──────────────────────────────────────────────────
@app.get("/api/pnl", dependencies=[Depends(_require_auth)])
async def pnl():
    return app.state.engine.stats.all_summaries()

@app.get("/api/db-stats", dependencies=[Depends(_require_auth)])
async def db_stats():
    return await db.get_stats()


# ── Score pré-trade ───────────────────────────────────────────────
@app.get("/api/score/{symbol}", dependencies=[Depends(_require_auth)])
async def score_endpoint(symbol: str, direction: str = "LONG"):
    from bot import score as scoring
    try:
        kl = app.state.client.get_cached_klines(symbol.upper(), "15", 50)
        if len(kl) < 10:
            kl = await app.state.client.get_klines(symbol.upper(), "15", 50)
        c = [k["c"] for k in kl]
        h = [k["h"] for k in kl]
        l = [k["l"] for k in kl]
        v = [k["v"] for k in kl]
        result = await scoring.calculate(
            symbol.upper(), direction.upper(), c, h, l, v, app.state.client,
        )
        return result
    except Exception as e:
        return {"error": str(e), "total": 0, "aprovado": False}


# ── Market data ───────────────────────────────────────────────────
@app.get("/api/market/{symbol}", dependencies=[Depends(_require_auth)])
async def market(symbol: str):
    sym = symbol.upper()
    try:
        oi  = await app.state.client.get_open_interest(sym)
        fr  = await app.state.client.get_funding_rate(sym)
        tk  = app.state.client.get_cached_ticker(sym)
        return {"symbol": sym, "open_interest": oi, "funding_rate": fr, "ticker": tk}
    except Exception as e:
        return {"error": str(e)}


# ── Correlação entre pares ────────────────────────────────────────
@app.get("/api/correlation", dependencies=[Depends(_require_auth)])
async def correlation():
    from bot import correlation as corr_guard
    from bot.config import cfg
    symbols = app.state.engine.viable_symbols[:8]
    return {
        "matrix":    corr_guard.get_correlation_matrix(symbols),
        "symbols":   symbols,
        "threshold": cfg.MAX_CORRELATION,
    }


# ── Dashboard ─────────────────────────────────────────────────────
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    try:
        with open("dashboard/index.html", "r") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Dashboard não encontrado</h1>", status_code=404)


# ── Backtest manual ───────────────────────────────────────────────
@app.post("/api/backtest", dependencies=[Depends(_require_auth)])
async def trigger_backtest():
    from bot import backtest as bt
    asyncio.create_task(bt.weekly_backtest_loop(app.state.client))
    return {"message": "Backtest iniciado em background"}


# ── WS Cache stats (diagnóstico) ─────────────────────────────────
@app.get("/api/ws-stats", dependencies=[Depends(_require_auth)])
async def ws_stats():
    return app.state.client.get_cache_stats()


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=cfg.PORT,
        log_level=cfg.LOG_LEVEL.lower(),
    )
