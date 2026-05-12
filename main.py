import os, asyncio, uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager

from bot.engine import TradingEngine
from bot.bybit import BybitClient
from bot.config import cfg
from bot.logger import log


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("🚀 NEXUS-7 iniciando...")
    client = BybitClient()
    engine = TradingEngine(client)
    app.state.client = client
    app.state.engine = engine
    asyncio.create_task(engine.run())
    log.info("✅ NEXUS-7 online")
    yield
    engine.stop()
    await client.close()


app = FastAPI(title="NEXUS-7", version="5.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


@app.get("/")
async def root():
    return {"status": "online", "version": "5.0.0", "name": "NEXUS-7 AI Trader"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/status")
async def status():
    return app.state.engine.get_status()


@app.get("/api/balance")
async def balance():
    b = await app.state.client.get_balance()
    return {"balance": b, "currency": "USDT"}


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
    with open("dashboard/index.html") as f:
        return f.read()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
