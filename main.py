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
    log.info("🚀 A007A TRADE v7 iniciando...")
    client = BybitClient()
    engine = TradingEngine(client)
    app.state.client = client
    app.state.engine = engine

    # Engine roda em background — NÃO bloqueia o health check
    asyncio.create_task(engine.run())

    log.info("✅ A007A TRADE online — aguardando health check")
    yield
    # Shutdown
    engine.stop()
    await asyncio.sleep(0.5)
    await client.close()
    log.info("👋 A007A TRADE encerrado")


app = FastAPI(title="A007A TRADE", version="7.1.0", lifespan=lifespan)
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
    return {"status": "online", "version": "7.1.0", "name": "A007A TRADE"}

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
