import os, asyncio, uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager

from bot.engine import TradingEngine
from bot.bybit import BybitClient
from bot.logger import log
from bot import database as db


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("🚀 BGX Capital iniciando...")
    client = BybitClient()
    engine = TradingEngine(client)
    app.state.client = client
    app.state.engine = engine
    asyncio.create_task(engine.run())
    log.info("✅ BGX Capital online")
    yield
    engine.stop()
    await asyncio.sleep(0.5)
    await client.close()


app = FastAPI(title="BGX Capital", version="10.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ── Health — SEMPRE 200 OK ───────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"status": "online", "version": "10.0.0", "name": "BGX Capital"}


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


@app.get("/api/whale-alerts")
async def whale_alerts():
    """Transações de baleias via CryptoQuant/Santiment públicos."""
    from bot.score import _news_cache, _macro_cache
    from bot.market_data import get_macro_summary
    macro = get_macro_summary()
    return {
        "btc_dominance":  _macro_cache.get("btc_dominance", 57.0),
        "fear_greed":     _macro_cache.get("fear_greed", 50),
        "dxy_trend":      macro.get("dxy_trend", "neutral"),
        "sp500_trend":    macro.get("sp500_trend", "neutral"),
        "btc_sp500_corr": macro.get("btc_sp500_corr", 0.7),
        "btc_dxy_corr":   macro.get("btc_dxy_corr", -0.6),
        "news_signal":    _news_cache.get("classificacao", "NEUTRO"),
        "news_conf":      _news_cache.get("score_confianca", 0),
        "fomc_window":    _news_cache.get("fomc_window", False),
        "note":           "Whale tracking via Glassnode/CryptoQuant requer API paga"
    }

@app.get("/api/indicators/{symbol}")
async def indicators(symbol: str, interval: str = "15"):
    """Retorna todos os indicadores calculados para um símbolo."""
    from bot.indicators import (adx, bollinger, choppiness, vwap as vwap_fn,
                                 volume_profile, delta_footprint, smc_analysis, atr)
    import numpy as np
    sym = symbol.upper()
    try:
        kl = app.state.client.get_cached_klines(sym, interval, 100)
        if len(kl) < 30:
            kl = await app.state.client.get_klines(sym, interval, 100)
        if not kl:
            return {"error": "Sem dados"}
        c=[k["c"] for k in kl]; h=[k["h"] for k in kl]
        l=[k["l"] for k in kl]; o=[k["o"] for k in kl]; v=[k["v"] for k in kl]
        atr_arr = atr(h,l,c)
        return {
            "symbol":   sym, "interval": interval,
            "price":    c[-1],
            "adx":      adx(h,l,c),
            "bollinger":bollinger(c),
            "choppiness":choppiness(h,l,c),
            "vwap":     vwap_fn(h,l,c,v),
            "vol_profile": volume_profile(h,l,v),
            "delta":    delta_footprint(c,v,o),
            "smc":      smc_analysis(h,l,c),
            "atr":      round(float(atr_arr[-1]),6),
            "atr_pct":  round(float(atr_arr[-1])/c[-1]*100, 3),
        }
    except Exception as e:
        return {"error": str(e)}

# ── Dashboard ────────────────────────────────────────────────────
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    try:
        with open("dashboard/index.html") as f:
            return f.read()
    except Exception:
        return "<h1>Dashboard não encontrado</h1>"



@app.get("/api/decisions")
async def decisions(limit: int = 60):
    """Retorna as últimas decisões do scan (SIGNAL e HOLD) para o dashboard."""
    try:
        from bot import database as db
        rows = await db.get_recent_decisions(limit)
        return {"decisions": rows}
    except Exception as e:
        # Fallback: retorna log em memória do engine
        eng = app.state.engine
        logs = []
        for sig in reversed(eng._signals_log[-limit:]):
            logs.append({
                "timestamp": sig.get("time", ""),
                "symbol":    sig.get("symbol", ""),
                "type":      "SIGNAL",
                "score":     sig.get("score", 0),
                "reason":    sig.get("reason", ""),
            })
        return {"decisions": logs}


@app.get("/api/test-notify")
async def test_notify():
    """Envia mensagem de teste no Telegram para verificar configuração."""
    from bot.notifier import notify
    from bot.config import cfg
    if not cfg.TELEGRAM_TOKEN:
        return {"ok": False, "error": "TELEGRAM_TOKEN não configurado no Railway"}
    if not cfg.TELEGRAM_CHAT:
        return {"ok": False, "error": "TELEGRAM_CHAT não configurado no Railway"}
    try:
        await notify(
            "✅ *BGX Capital — Teste OK!*\n"
            "`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
            "🤖 Bot conectado e enviando alertas\n"
            f"💬 Chat ID: `{cfg.TELEGRAM_CHAT}`\n"
            "`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
            "_Configuração do Telegram funcionando!_"
        )
        return {"ok": True, "message": "Mensagem enviada!", "chat": cfg.TELEGRAM_CHAT}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Telegram Webhook — comandos /status /balance /positions /pause /resume ────
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Recebe comandos do Telegram e responde."""
    from bot.notifier import notify
    from bot.config import cfg
    try:
        data = await request.json()
        msg  = data.get("message") or data.get("edited_message", {})
        if not msg:
            return {"ok": True}
        text    = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))

        # Só responde ao chat autorizado
        if chat_id != cfg.TELEGRAM_CHAT:
            return {"ok": True}

        eng    = app.state.engine
        client = app.state.client

        if text.startswith("/status"):
            st  = eng.get_status()
            bal = await client.get_balance()
            resp_text = (
                f"🤖 *BGX Capital — STATUS*\n"
                f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
                f"{'🟢 ATIVO' if st.get('running') else '🔴 PAUSADO'}\n"
                f"💼 Saldo:        `${bal:,.2f} USDT`\n"
                f"⚡ Poder compra: `${bal*cfg.LEVERAGE:,.2f} USDT`\n"
                f"📊 Posições:     `{st.get('open_positions',0)}/{cfg.MAX_POSITIONS}`\n"
                f"🎯 PnL hoje:     `${st.get('daily_pnl',0):+.2f} USDT`\n"
                f"📈 Score mín:    `{cfg.MIN_ENTRY_SCORE}/100`\n"
                f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`"
            )
        elif text.startswith("/balance"):
            bal = await client.get_balance()
            resp_text = (
                f"💼 *SALDO BGX Capital*\n"
                f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
                f"💰 Saldo:        `${bal:,.2f} USDT`\n"
                f"⚡ Poder compra: `${bal*cfg.LEVERAGE:,.2f} USDT`\n"
                f"📊 Alavancagem:  `{cfg.LEVERAGE}x`"
            )
        elif text.startswith("/positions"):
            positions = list(eng.positions.values())
            if not positions:
                resp_text = "📭 *Nenhuma posição aberta no momento*"
            else:
                lines = [f"📊 *POSIÇÕES ABERTAS ({len(positions)})*\n`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`"]
                for p in positions:
                    icon = "🟢" if p.direction == "LONG" else "🔴"
                    lines.append(
                        f"{icon} `{p.symbol}` {p.direction}\n"
                        f"   Entrada: `${p.entry:,.4f}` → Atual: `${p.current_price:,.4f}`\n"
                        f"   PnL: `{'+' if p.pnl>=0 else ''}${p.pnl:,.2f}` ({p.pnl_pct():+.1f}%)"
                    )
                resp_text = "\n".join(lines)
        elif text.startswith("/pause"):
            eng.stop()
            resp_text = "⏸ *Bot pausado!*\nUse /resume para retomar."
        elif text.startswith("/resume"):
            import asyncio
            asyncio.create_task(eng.run())
            resp_text = "▶️ *Bot retomado!*\nEscaneando o mercado..."
        elif text.startswith("/pnl"):
            st = eng.get_status()
            resp_text = (
                f"📈 *PnL BGX Capital*\n"
                f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
                f"📅 Hoje:    `${st.get('daily_pnl',0):+.2f} USDT`\n"
                f"📆 Sessão:  `${st.get('session_pnl',0):+.2f} USDT`\n"
                f"🏆 Trades:  `{st.get('total_trades',0)}`\n"
                f"✅ Win Rate: `{st.get('win_rate',0):.0f}%`"
            )
        elif text.startswith("/help"):
            resp_text = (
                f"🤖 *BGX Capital — Comandos*\n"
                f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
                f"/status — status completo do bot\n"
                f"/balance — saldo e poder de compra\n"
                f"/positions — posições abertas\n"
                f"/pnl — lucro/prejuízo\n"
                f"/pause — pausar o bot\n"
                f"/resume — retomar o bot\n"
                f"/help — esta mensagem"
            )
        else:
            return {"ok": True}

        await notify(resp_text)
    except Exception as e:
        log.error(f"telegram_webhook: {e}")
    return {"ok": True}


@app.get("/api/news")
async def get_news(limit: int = 20, sentiment: str = ""):
    """Retorna notícias processadas pelo pipeline com score de impacto."""
    from bot.news_pipeline import _pipeline_cache, get_pipeline_status
    items = _pipeline_cache
    if sentiment:
        items = [i for i in items if i.sentiment.upper() == sentiment.upper()]
    return {
        "news": [
            {
                "title":      i.title,
                "source":     i.source,
                "tier":       i.source_tier,
                "sentiment":  i.sentiment,
                "confidence": i.confidence,
                "impact":     i.impact,
                "relevance":  i.relevance,
                "entities":   i.entities[:5],
                "url":        i.url,
                "age_min":    int(((__import__("time").time() - i.timestamp)) / 60),
            }
            for i in items[:limit]
        ],
        "status": get_pipeline_status(),
    }


@app.get("/api/news/impact")
async def get_news_impact_endpoint(direction: str = "LONG", symbol: str = "BTC"):
    """Retorna impacto das notícias para uma direção e símbolo específico."""
    from bot.news_pipeline import get_news_impact
    return get_news_impact(direction, symbol)


@app.post("/api/news/refresh")
async def refresh_news():
    """Força atualização imediata do pipeline de notícias."""
    from bot.news_pipeline import run_news_pipeline, _pipeline_last_run
    import bot.news_pipeline as np_mod
    np_mod._pipeline_last_run = 0   # força re-execução
    import asyncio
    asyncio.create_task(run_news_pipeline())
    return {"ok": True, "message": "Pipeline de notícias será atualizado em breve"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
