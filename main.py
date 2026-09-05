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
from bot.kucoin import KuCoinClient as ExchangeClient, PAPER_TRADE, TRADING_MODE_REASON
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

    # ══════════════════════════════════════════════════════════════
    # SELF-CHECK DE INTEGRIDADE (previne bugs silenciosos)
    #
    # Vários bugs deste projeto só apareceram após horas em produção
    # porque erros de programação eram engolidos por except genérico.
    # Esta verificação roda em ~200ms e detecta, ANTES de operar:
    #   • NameError latente (ex: aiohttp usado sem import)
    #   • AttributeError latente (self.metodo() inexistente)
    #   • métodos duplicados que se sobrescrevem
    #   • combinações de config matematicamente impossíveis
    #
    # Bug crítico → bot inicia em modo BLOQUEADO. O /health continua
    # respondendo (o deploy não falha), mas nenhuma ordem é enviada.
    # ══════════════════════════════════════════════════════════════
    _blocked_by_selfcheck = False
    try:
        from bot.selfcheck import run_selfcheck
        _report = run_selfcheck()
        if _report["critical"]:
            _blocked_by_selfcheck = True
            log.critical(
                f"🚫 OPERAÇÃO BLOQUEADA: {len(_report['critical'])} bug(s) "
                f"crítico(s) detectado(s) no código. Corrija antes de operar."
            )
    except Exception as _e:
        _blocked_by_selfcheck = True
        log.critical(
            f"🚫 OPERAÇÃO BLOQUEADA: self-check falhou e não pôde "
            f"confirmar a integridade do código: {_e}"
        )
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

        if _blocked_by_selfcheck:
            # Não inicia o engine: bug de código não vai a produção.
            app.state.blocked = True
            log.critical(
                "🚫 Engine NÃO iniciado — self-check encontrou bugs críticos. "
                "Veja os logs acima. Corrija e faça redeploy."
            )
            try:
                from bot.notifier import notify as _n
                await _n(
                    "🚫 *BOT BLOQUEADO NO STARTUP*\n"
                    "O self-check detectou bug(s) crítico(s) no código.\n"
                    "Nenhuma ordem será enviada até a correção.\n"
                    "_Verifique os logs do Railway._"
                )
            except Exception:
                pass
            return

        # Verifica o canal do Telegram ANTES de iniciar o engine.
        # Falha aqui não impede a operação — apenas registra o motivo.
        try:
            from bot.notifier import test_telegram
            app.state.telegram = await asyncio.wait_for(test_telegram(), timeout=10)
        except Exception as _e:
            log.warning(f"test_telegram: {_e}")
            app.state.telegram = {"ok": False, "reason": str(_e)}

        app.state.blocked = False
        app.state.engine_task = asyncio.create_task(engine.run())
        log.info("✅ BGX Capital online (KuCoin Futures)")

        # Avisa no Telegram em qual modo o bot subiu — evita a situação
        # de esperar ordens que nunca virão por falta de configuração.
        try:
            from bot.notifier import notify as _n
            if PAPER_TRADE:
                await _n(
                    f"🟡 *BOT ONLINE — MODO SIMULAÇÃO*\n"
                    f"`{'━'*26}`\n"
                    f"⚠️ *NENHUMA ordem será enviada à KuCoin*\n\n"
                    f"Motivo: _{TRADING_MODE_REASON}_\n\n"
                    f"Para operar de verdade, defina no Railway:\n"
                    f"`PAPER_TRADE=false`\n"
                    f"`LIVE_TRADING_CONFIRMED=I_UNDERSTAND_THE_RISK`"
                )
            else:
                await _n(
                    f"🔴 *BOT ONLINE — OPERAÇÃO REAL*\n"
                    f"`{'━'*26}`\n"
                    f"Ordens serão enviadas à KuCoin com capital real."
                )
        except Exception as _e:
            log.debug(f"notify modo: {_e}")

    app.state.bootstrap_task = asyncio.create_task(_bootstrap())

    # yield IMEDIATO — /health passa a responder agora, sem esperar a KuCoin
    yield

    # ── Shutdown ordenado ─────────────────────────────────────────
    # P2 CORRIGIDO: a conexão com o banco nunca era fechada. Em restarts
    # frequentes, conexões PostgreSQL ficavam penduradas até o timeout do
    # servidor, consumindo slots do pool.
    engine.stop()
    for t in ("bootstrap_task", "engine_task"):
        task = getattr(app.state, t, None)
        if task and not task.done():
            task.cancel()
    await asyncio.sleep(1.0)
    try:
        await client.close()
    except Exception as e:
        log.warning(f"Erro ao fechar cliente: {e}")
    try:
        await db.close()
    except Exception as e:
        log.warning(f"Erro ao fechar banco: {e}")
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
        "blocked":  bool(getattr(app.state, "blocked", False)),
        # Modo de operação exposto aqui para responder rapidamente
        # "por que o bot não abre ordens?"
        "telegram":     getattr(app.state, "telegram", {"ok": None}),
        "trading_mode": "PAPER" if PAPER_TRADE else "LIVE",
        "mode_reason":  TRADING_MODE_REASON,
        "orders_sent_to_exchange": not PAPER_TRADE,
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

