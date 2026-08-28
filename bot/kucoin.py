"""
BGX Capital — KuCoin Futures Client v1.0
Substitui bot/bybit.py completamente.

Implementa a mesma interface pública do BybitClient:
  - get_balance()
  - place_order(symbol, side, qty, sl, tp, instruments)
  - set_sl(symbol, sl, instruments)
  - cancel_all_orders(symbol)
  - get_klines(symbol, interval, limit)
  - get_cached_klines(symbol, interval, limit)
  - get_ticker(symbol)
  - get_cached_ticker(symbol)
  - get_open_interest(symbol)
  - get_funding_rate(symbol)
  - get_orderbook(symbol)
  - get_positions()
  - set_leverage(symbol, leverage)
  - start_websocket(symbols, intervals)
  - get_cache_stats()
  - close()

KuCoin Futures API v1: https://docs.kucoin.com/futures
WebSocket: wss://ws-api.kucoin.com/endpoint (token-based)

Diferenças-chave KuCoin vs Bybit:
  - Autenticação: KC-API-KEY, KC-API-SIGN, KC-API-TIMESTAMP, KC-API-PASSPHRASE
  - WS requer token REST obtido via POST /api/v1/bullet-public (ou private)
  - Símbolo: XBTUSDTM (BTC), ETHUSDTM (ETH) — sufixo M para USDT margined
  - Leverage: POST /api/v1/position/margin/auto-deposit-status (diferente da Bybit)
  - Funding a cada 8h (igual Bybit)
  - OrderLinkId: clientOid (máx 40 chars, apenas alfanumérico + hífen)
"""

import asyncio, hashlib, hmac, json, math, os, time
from base64 import b64encode
from collections import deque
from typing import Optional

import aiohttp

from bot.logger import log

# ── Credenciais ────────────────────────────────────────────────────
# Strip whitespace from credentials to prevent auth failures
API_KEY        = os.environ.get("KUCOIN_API_KEY",        "").strip()
API_SECRET     = os.environ.get("KUCOIN_API_SECRET",     "").strip()
API_PASSPHRASE = os.environ.get("KUCOIN_API_PASSPHRASE", "").strip()
PAPER_TRADE    = os.environ.get("PAPER_TRADE", "false").lower() == "true"

# ── Endpoints ─────────────────────────────────────────────────────
REST_BASE = "https://api-futures.kucoin.com"
WS_BASE   = "wss://ws-api.kucoin.com/endpoint"

# ── Constantes ────────────────────────────────────────────────────
TAKER_FEE = 0.0006   # 0.06% taker (KuCoin Futures padrão)

# ── Mapa de símbolos Bybit → KuCoin ──────────────────────────────
# KuCoin Futures USDT-margined usa sufixo "M" (ex: XBTUSDTM, ETHUSDTM)
SYMBOL_MAP = {
    "BTCUSDT":  "XBTUSDTM",
    "ETHUSDT":  "ETHUSDTM",
    "SOLUSDT":  "SOLUSDTM",
    "BNBUSDT":  "BNBUSDTM",
    "XRPUSDT":  "XRPUSDTM",
    "ADAUSDT":  "ADAUSDTM",
    "DOGEUSDT": "DOGEUSDTM",
    "LINKUSDT": "LINKUSDTM",
    "AVAXUSDT": "AVAXUSDTM",
    "POLUSDT":  "POLUSDTM",
    "DOTUSDT":  "DOTUSDTM",
    "LTCUSDT":  "LTCUSDTM",
}
# Mapa reverso: KuCoin → Bybit (para padronizar respostas)
SYMBOL_MAP_REV = {v: k for k, v in SYMBOL_MAP.items()}

def to_kucoin(symbol: str) -> str:
    """Converte símbolo Bybit (BTCUSDT) → KuCoin (XBTUSDTM)."""
    return SYMBOL_MAP.get(symbol, symbol)

def to_standard(symbol: str) -> str:
    """Converte símbolo KuCoin (XBTUSDTM) → padrão (BTCUSDT)."""
    return SYMBOL_MAP_REV.get(symbol, symbol)

# ── Intervalo: Bybit → KuCoin ────────────────────────────────────
INTERVAL_MAP = {
    "1": 1, "3": 3, "5": 5, "15": 15, "30": 30,
    "60": 60, "120": 120, "240": 240, "D": 1440, "W": 10080,
}

