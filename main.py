"""
BGX Capital — API Server v12.1 (KuCoin)
Única mudança em relação à versão Bybit:
  - Import: KuCoinClient em vez de BybitClient
  - Variável de ambiente: KUCOIN_API_KEY/SECRET/PASSPHRASE
  - Tudo mais (auth, CORS, endpoints, rate limit) idêntico
"""
import asyncio, os, time
from collections import defaultdict
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# ── ÚNICA LINHA ALTERADA em relação à versão Bybit ───────────────
from bot.kucoin import KuCoinClient as ExchangeClient, PAPER_TRADE
# ─────────────────────────────────────────────────────────────────

from bot.engine import TradingEngine
from bot.config import cfg
from bot.logger import log
from bot import database as db


# ── Autenticação ──────────────────────────────────────────────────
_bearer = HTTPBearer(auto_error=False)

def _require_auth(credentials: HTTPAuthorizationCredentials = Depends(_bearer)):
    secret = cfg.BOT_API_SECRET
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="BOT_API_SECRET não configurado — configure no Railway antes de usar a API."
        )
    if not credentials or credentials.credentials != secret:
        raise HTTPException(status_code=401, detail="Token inválido ou ausente")


# ── Rate Limiting ─────────────────────────────────────────────────
_rate_counters: dict = defaultdict(list)

def _rate_limit(request: Request):
    ip  = request.client.host if request.client else "unknown"
    now = time.time()
    _rate_counters[ip] = [t for t in _rate_counters[ip] if now - t < 60]
    if len(_rate_counters[ip]) >= 10:
        raise HTTPException(status_code=429, detail="Rate limit — aguarde 1 minuto")
    _rate_counters[ip].append(now)


# ── Lifespan ──────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    BUG CORRIGIDO (deploy travado no Railway):
    'await client.load_instruments()' era executado ANTES do yield. O FastAPI
    só passa a responder requisições depois que o lifespan atinge o yield —
    então o endpoint /health ficava indisponível enquanto o carregamento
    rodava. Qualquer lentidão ou retry na API da KuCoin (o _get tem 3
    tentativas com backoff exponencial) estourava o healthcheckTimeout do
    Railway e o deploy ficava preso em "carregando" até falhar.

    Agora: o servidor sobe imediatamente e /health responde de cara.
    O carregamento de instrumentos vai para uma task de background com
    timeout, e o engine só inicia depois que ela conclui.
    """
    log.info("🚀 BGX Capital v12.1 (KuCoin) iniciando...")
    if PAPER_TRADE:
        log.warning("🟡 PAPER TRADE MODE ATIVO")

    client = ExchangeClient()
    engine = TradingEngine(client)
    app.state.client = client
    app.state.engine = engine
    app.state.ready  = False

    async def _bootstrap():
        """Carrega instrumentos e inicia o engine — fora do caminho do healthcheck."""
        try:
            await asyncio.wait_for(client.load_instruments(), timeout=45)
            log.info("✅ Instrumentos carregados")
        except asyncio.TimeoutError:
            log.error("⏱️ load_instruments excedeu 45s — seguindo mesmo assim")
        except Exception as e:
            log.error(f"❌ load_instruments falhou: {e} — seguindo mesmo assim")

        app.state.ready = True
        app.state.engine_task = asyncio.create_task(engine.run())
        log.info("✅ BGX Capital online (KuCoin Futures)")

    app.state.bootstrap_task = asyncio.create_task(_bootstrap())

    # yield IMEDIATO — /health passa a responder agora, sem esperar a KuCoin
    yield

    engine.stop()
    for t in ("bootstrap_task", "engine_task"):
        task = getattr(app.state, t, None)
        if task and not task.done():
            task.cancel()
    await asyncio.sleep(1.0)
    await client.close()
    log.info("👋 Encerrado")


app = FastAPI(title="BGX Capital KuCoin", version="12.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=False,
)


# ── Health ────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    """
    Healthcheck do Railway. NUNCA faz I/O externo — responde sempre 200
    assim que o processo está de pé. O campo 'ready' indica se o bootstrap
    (carregamento de instrumentos + engine) já concluiu.
    """
    return {
        "status":   "ok",
        "version":  "12.1.0",
        "exchange": "kucoin",
        "ready":    bool(getattr(app.state, "ready", False)),
    }

@app.get("/")
async def root():
    return {"status": "online", "version": "12.1.0", "exchange": "kucoin"}


# ── Status / Saldo / Posições ─────────────────────────────────────
@app.get("/api/status", dependencies=[Depends(_require_auth)])
async def status():
    return app.state.engine.get_status()

@app.get("/api/balance", dependencies=[Depends(_require_auth)])
async def balance():
    b = await app.state.client.get_balance()
    return {"balance": b, "currency": "USDT", "exchange": "kucoin"}

@app.get("/api/positions", dependencies=[Depends(_require_auth)])
async def positions():
    eng = app.state.engine
    return {
        "open":     [p.to_dict() for p in eng.positions.values()],
        "count":    len(eng.positions),
        "exchange": "kucoin",
    }


# ── Controle ──────────────────────────────────────────────────────
@app.post("/api/pause", dependencies=[Depends(_require_auth), Depends(_rate_limit)])
async def pause(request: Request):
    app.state.engine.stop()
    return {"message": "Bot pausado"}

@app.post("/api/resume", dependencies=[Depends(_require_auth), Depends(_rate_limit)])
async def resume(request: Request):
    engine = app.state.engine
    task   = getattr(app.state, "engine_task", None)
    if task and not task.done():
        engine.active = True
        return {"message": "Bot reativado"}
    app.state.engine_task = asyncio.create_task(engine.run())
    return {"message": "Bot retomado"}

@app.post("/api/close-all", dependencies=[Depends(_require_auth), Depends(_rate_limit)])
async def close_all(request: Request):
    engine = app.state.engine
    engine.stop()
    result = await engine.close_all_positions()
    return {"message": f"Emergency close: {result['closed']} posições fechadas", **result}


# ── PnL / Stats ───────────────────────────────────────────────────
@app.get("/api/pnl", dependencies=[Depends(_require_auth)])
async def pnl():
    return app.state.engine.stats.all_summaries()

@app.get("/api/db-stats", dependencies=[Depends(_require_auth)])
async def db_stats():
    return await db.get_stats()


# ── Score ─────────────────────────────────────────────────────────
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


# ── Correlação ────────────────────────────────────────────────────
@app.get("/api/correlation", dependencies=[Depends(_require_auth)])
async def correlation():
    from bot import correlation as corr_guard
    symbols = app.state.engine.viable_symbols[:8]
    return {
        "matrix":    corr_guard.get_correlation_matrix(symbols),
        "symbols":   symbols,
        "threshold": cfg.MAX_CORRELATION,
    }


# ── WS Cache stats ────────────────────────────────────────────────
@app.get("/api/ws-stats", dependencies=[Depends(_require_auth)])
async def ws_stats():
    return app.state.client.get_cache_stats()


# ── Dashboard ─────────────────────────────────────────────────────
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    try:
        with open("dashboard/index.html") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>Dashboard não encontrado</h1>", status_code=404)


# ── Backtest manual ───────────────────────────────────────────────
@app.post("/api/backtest", dependencies=[Depends(_require_auth)])
async def trigger_backtest():
    from bot import backtest as bt
    asyncio.create_task(bt.weekly_backtest_loop(app.state.client))
    return {"message": "Backtest iniciado"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=cfg.PORT,
                log_level=cfg.LOG_LEVEL.lower())
