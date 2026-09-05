"""
BGX Capital — API Server v12.1 (KuCoin)
Única mudança em relação à versão Bybit:
  - Import: KuCoinClient em vez de BybitClient
  - Variável de ambiente: KUCOIN_API_KEY/SECRET/PASSPHRASE
  - Tudo mais (auth, CORS, endpoints, rate limit) idêntico
"""
import asyncio, builtins, os, time
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
    _sitecustomize_status = getattr(
        builtins, "_nexus_sitecustomize_status", "not_loaded"
    )
    if _sitecustomize_status != "ok":
        _blocked_by_selfcheck = True
        log.critical(
            "🚫 OPERAÇÃO BLOQUEADA: hardenings de startup não foram "
            f"confirmados (sitecustomize={_sitecustomize_status})"
        )
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

@app.post("/api/test-telegram", dependencies=[Depends(_require_auth)])
async def test_telegram_endpoint():
    """Envia uma mensagem de teste ao Telegram e reporta o resultado."""
    from bot.notifier import test_telegram, notify
    res = await test_telegram()
    if res.get("ok"):
        await notify(
            "🧪 *TESTE DE CONEXÃO*\n"
            "Se você está lendo isto, o canal está funcionando."
        )
        res["mensagem_enviada"] = True
    return res


@app.get("/api/integrity", dependencies=[Depends(_require_auth)])
async def integrity_status():
    """
    Estado do kill switch de integridade (Fase 3, P0).

    Responde: o bot PODE abrir novas posições agora? Se não, por quê?
    """
    eng = app.state.engine
    g = getattr(eng, "integrity", None)
    if g is None:
        return {"error": "IntegrityGuard não inicializado"}
    d = g.to_dict()
    d["orders_tracked"] = len(getattr(eng, "orders", []) or [])
    return d


@app.get("/api/liquidation-check", dependencies=[Depends(_require_auth)])
async def liquidation_check(entry: float, stop: float, leverage: int = 0,
                            side: str = "LONG"):
    """
    Calcula se um stop é efetivo para dado leverage, incluindo margem de
    manutenção, taxas e slippage. Útil para dimensionar o leverage antes
    de configurá-lo.
    """
    from bot import liquidation as liq
    lev = leverage or cfg.LEVERAGE
    a = liq.analyze(entry, stop, lev, side.upper() == "LONG")
    out = a.to_dict()
    out["leverage"] = lev
    out["max_leverage_para_este_stop"] = liq.max_leverage_for_stop(a.stop_move_pct)
    return out


@app.get("/api/why-no-trade", dependencies=[Depends(_require_auth)])
async def why_no_trade():
    """
    Responde diretamente: por que o bot não está operando?

    Mostra o modo de operação, os thresholds ativos e a distribuição
    dos scores recentes — separando "não há setup bom" (mercado) de
    "algo está bloqueando" (configuração ou bug).
    """
    eng = app.state.engine
    mn  = cfg.MIN_ENTRY_SCORE

    # Buffer do strategy: registra TODO score avaliado, não só os aprovados
    try:
        from bot.strategy import get_score_log
        _log   = get_score_log(300)
        scores = [x["score"] for x in _log]
    except Exception:
        _log   = []
        scores = list(getattr(eng, "_score_hist", []))

    if scores:
        dist = {
            "avaliados":       len(scores),
            "maximo":          max(scores),
            "media":           round(sum(scores) / len(scores), 1),
            "acima_do_minimo": len([s for s in scores if s >= mn]),
            "a_5pts_ou_menos": len([s for s in scores if mn - 5 <= s < mn]),
        }
        if dist["acima_do_minimo"] > 0:
            veredito = "Sinais passaram o score — verifique vetos do NEXUS AI e do Risk Engine"
        elif dist["maximo"] >= mn - 5:
            veredito = f"Chegou perto (máx {dist['maximo']}), mas nenhum atingiu {mn}"
        else:
            veredito = f"Nenhum setup próximo do mínimo (máx {dist['maximo']} vs {mn})"
    else:
        dist = {"avaliados": 0}
        veredito = "Nenhum sinal avaliado ainda — bot pode ter acabado de subir"

    return {
        # Os 10 pares mais recentes avaliados, com o detalhe por timeframe
        "ultimos_avaliados": [
            {"par": x["symbol"], "score": x["score"],
             "4H": x["s4h"], "1H": x["s1h"], "15M": x["s15"]}
            for x in _log[:10]
        ],
        "modo":            "PAPER" if PAPER_TRADE else "LIVE",
        "ordens_reais":    not PAPER_TRADE,
        "thresholds": {
            "score_minimo":  mn,
            "nexus_minimo":  float(os.environ.get("NEXUS_MIN_SCORE", "55")),
            "rr_minimo":     cfg.MIN_RR_RATIO,
            "sl_max_pct":    round(100 / max(1, cfg.LEVERAGE) * 0.75, 2),
            "filtro_liquidacao_ativo": os.environ.get(
                "ALLOW_SL_BEYOND_LIQUIDATION", "false").lower() != "true",
            "liquidacao_pct": round(100 / max(1, cfg.LEVERAGE), 2),
        },
        "scores_recentes": dist,
        "posicoes_abertas": len(getattr(eng, "positions", {})),
        "max_posicoes":     cfg.MAX_POSITIONS,
        "pares_monitorados": len(getattr(eng, "viable_symbols", [])),
        "veredito":         veredito,
    }


@app.get("/api/selfcheck", dependencies=[Depends(_require_auth)])
async def selfcheck():
    """
    Verificação de integridade do código sob demanda.

    Detecta NameError/AttributeError latentes, métodos duplicados e
    combinações de configuração impossíveis — a classe de bug que
    historicamente passou despercebida por horas em produção.
    """
    from bot.selfcheck import run_selfcheck
    rep = run_selfcheck(verbose=False)
    return {
        "status":         "BLOCKED" if rep["critical"] else "OK",
        "files_checked":  rep["files_checked"],
        "critical_count": len(rep["critical"]),
        "critical":       rep["critical"],
        "warnings":       rep["warning"],
        "engine_blocked": bool(getattr(app.state, "blocked", False)),
    }


@app.get("/api/nexus/{symbol}", dependencies=[Depends(_require_auth)])
async def nexus_decision(symbol: str, direction: str = "LONG"):
    """
    NEXUS AI — decisão completa para um símbolo (seções 18, 23).

    Retorna a estrutura padronizada com score, regime, EV, R:R líquido,
    reasoning, warnings e a saída de cada um dos 7 modelos do ensemble.
    Útil para entender POR QUE a IA aprovou ou vetou um setup.
    """
    from bot import nexus_ai
    eng = app.state.engine
    sym = symbol.upper()
    try:
        k15 = eng.client.get_cached_klines(sym, "15",  200)
        k1h = eng.client.get_cached_klines(sym, "60",  100)
        k4h = eng.client.get_cached_klines(sym, "240",  60)
        if len(k15) < 60:
            k15 = await eng.client.get_klines(sym, "15", 200)
        if len(k1h) < 40:
            k1h = await eng.client.get_klines(sym, "60", 100)
        if len(k4h) < 20:
            k4h = await eng.client.get_klines(sym, "240", 60)

        px = float(k15[-1]["c"]) if k15 else 0.0
        # Níveis ilustrativos apenas para avaliação via endpoint
        if direction.upper() == "LONG":
            sl, tp = px * 0.985, px * 1.03
        else:
            sl, tp = px * 1.015, px * 0.97

        funding = None
        try:
            funding = await eng.client.get_funding_rate(sym)
        except Exception as _e:
            log.debug(f"endpoint /api/nexus: funding indisponível: {_e}")

        d = nexus_ai.decide(
            symbol=sym, k15=k15, k1h=k1h, k4h=k4h,
            entry=px, sl=sl, tp=tp,
            ticker=eng.client.get_cached_ticker(sym) or None,
            funding=funding,
        )
        return d.to_dict()
    except Exception as e:
        return {"symbol": sym, "error": str(e),
                "decision": "WAIT", "execution_allowed": False}


@app.get("/api/nexus", dependencies=[Depends(_require_auth)])
async def nexus_last():
    """Últimas decisões do NEXUS AI por símbolo (seção 23)."""
    eng = app.state.engine
    return {
        "enabled":   getattr(eng, "_last_nexus", None) is not None,
        "min_score": float(os.environ.get("NEXUS_MIN_SCORE", "85")),
        "decisions": getattr(eng, "_last_nexus", {}),
    }


@app.get("/api/expectancy", dependencies=[Depends(_require_auth)])
async def expectancy(days: int = 0):
    """
    ITEM 4: métricas que realmente medem edge — não o win rate isolado.

    Retorna expectancy em R, payoff ratio, profit factor e o win rate de
    BREAKEVEN para o payoff observado. A comparação win_rate vs
    breakeven_wr é o que diz se a estratégia é lucrativa.

    Exemplos:
      40% acerto, payoff 2.0 → breakeven 33% → lucrativo (+7pp)
      90% acerto, payoff 0.3 → breakeven 77% → lucrativo (+13pp) mas com
      expectancy MENOR e cauda de perdas muito pior.
    """
    return await db.get_expectancy_stats(days or None)


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
