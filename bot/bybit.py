"""
BGX Capital — Bybit V5 Client
- HMAC-SHA256 corrigido (recv_window consistente)
- REST para ordens e account
- WebSocket para dados de mercado em tempo real
- Reconexão automática
"""
import asyncio, hashlib, hmac, json, time, os
import aiohttp
from bot.logger import log
from bot.indicators import update_orderbook_history

BASE        = "https://api.bybit.com"
WS_PUBLIC   = "wss://stream.bybit.com/v5/public/linear"
WS_PRIVATE  = "wss://stream.bybit.com/v5/private"

API_KEY    = os.environ.get("BYBIT_API_KEY",    os.environ.get("BINANCE_API_KEY",    ""))
API_SECRET = os.environ.get("BYBIT_API_SECRET", os.environ.get("BINANCE_API_SECRET", ""))
RECV_WINDOW = "20000"

# ── Paper Trading Mode ───────────────────────────────────────────
PAPER_TRADE = os.environ.get("PAPER_TRADE", "false").lower() == "true"
if PAPER_TRADE:
    log.info("🟡 PAPER TRADE MODE ATIVO — nenhuma ordem real será enviada")


INTERVALS = {
    "1":"1","3":"3","5":"5","15":"15","30":"30",
    "60":"60","120":"120","240":"240","D":"D",
    "1m":"1","3m":"3","5m":"5","15m":"15","30m":"30",
    "1h":"60","2h":"120","4h":"240","1d":"D",
}


# ── HMAC-SHA256 corrigido ────────────────────────────────────────
def _sign(secret: str, payload: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _auth_params(params_str: str = "") -> dict:
    ts = str(int(time.time() * 1000))
    pre = ts + API_KEY + RECV_WINDOW + params_str
    sig = _sign(API_SECRET, pre)
    return {
        "X-BAPI-API-KEY":     API_KEY,
        "X-BAPI-TIMESTAMP":   ts,
        "X-BAPI-SIGN":        sig,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "Content-Type":       "application/json",
    }


class BybitClient:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        # WebSocket state
        self._ws_task     = None
        self._ws_handlers = {}   # topic → callback
        self._kline_cache = {}   # "symbol_interval" → list of klines
        self._ticker_cache= {}   # symbol → ticker dict

    def _sess(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── REST helpers ────────────────────────────────────────────
    async def _get(self, path: str, params: dict = None, auth: bool = False) -> dict:
        url = BASE + path
        qs  = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        headers = _auth_params(qs) if auth else {}
        try:
            async with self._sess().get(
                url, params=params, headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                data = await r.json()
            if data.get("retCode", 0) != 0:
                raise RuntimeError(f"Bybit {data.get('retCode')}: {data.get('retMsg')}")
            return data.get("result", {})
        except Exception as e:
            raise RuntimeError(f"GET {path}: {e}")

    async def _post(self, path: str, body: dict = None) -> dict:
        url     = BASE + path
        payload = json.dumps(body or {})
        headers = _auth_params(payload)
        try:
            async with self._sess().post(
                url, data=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                data = await r.json()
            if data.get("retCode", 0) not in (0, 110043):
                raise RuntimeError(f"Bybit {data.get('retCode')}: {data.get('retMsg')}")
            return data.get("result", {})
        except Exception as e:
            raise RuntimeError(f"POST {path}: {e}")

    # ── Public REST ─────────────────────────────────────────────
    async def ping(self) -> bool:
        try:
            await self._get("/v5/market/time")
            return True
        except Exception:
            return False

    async def get_klines(self, symbol: str, interval: str = "15", limit: int = 100) -> list:
        iv  = INTERVALS.get(interval, interval)
        res = await self._get("/v5/market/kline", {
            "category": "linear", "symbol": symbol,
            "interval": iv, "limit": limit,
        })
        raw = res.get("list", [])
        # Bybit retorna newest first → reverter
        raw = list(reversed(raw))
        return [
            {"o": float(k[1]), "h": float(k[2]),
             "l": float(k[3]), "c": float(k[4]),
             "v": float(k[5])}
            for k in raw
        ]

    async def get_all_tickers(self) -> list:
        res = await self._get("/v5/market/tickers", {"category": "linear"})
        return res.get("list", [])

    async def get_instruments(self) -> dict:
        res = await self._get("/v5/market/instruments-info", {
            "category": "linear", "limit": "1000",
        })
        out = {}
        for item in res.get("list", []):
            sym = item.get("symbol", "")
            lot = item.get("lotSizeFilter", {})
            out[sym] = {
                "minQty":      float(lot.get("minOrderQty",  0.001)),
                "qtyStep":     float(lot.get("qtyStep",      0.001)),
                "minNotional": float(lot.get("minOrderAmt",  1.0)),
            }
        return out

    async def get_orderbook(self, symbol: str) -> dict:
        res = await self._get("/v5/market/orderbook", {
            "category": "linear", "symbol": symbol, "limit": "25",
        })
        return res

    async def get_open_interest(self, symbol: str) -> dict:
        res = await self._get("/v5/market/open-interest", {
            "category": "linear", "symbol": symbol,
            "intervalTime": "5min", "limit": "10",
        })
        return res.get("list", [{}])[0] if res.get("list") else {}

    async def get_funding_rate(self, symbol: str) -> float:
        res = await self._get("/v5/market/tickers", {
            "category": "linear", "symbol": symbol,
        })
        items = res.get("list", [])
        if items:
            return float(items[0].get("fundingRate", 0))
        return 0.0

    # ── Private REST ────────────────────────────────────────────
    async def get_balance(self) -> float:
        if not API_KEY:
            return 1000.0
        try:
            res = await self._get("/v5/account/wallet-balance",
                                  {"accountType": "UNIFIED"}, auth=True)
            for coin in res.get("list", [{}])[0].get("coin", []):
                if coin.get("coin") == "USDT":
                    return float(coin.get("walletBalance", 0))
            return 0.0
        except Exception as e:
            log.error(f"get_balance: {e}")
            return -1.0

    async def get_positions(self, symbol: str = None) -> list:
        if not API_KEY:
            return []
        params = {"category": "linear", "settleCoin": "USDT", "limit": "200"}
        if symbol:
            params["symbol"] = symbol
        try:
            res = await self._get("/v5/position/list", params, auth=True)
            return [p for p in res.get("list", []) if float(p.get("size", 0)) > 0]
        except Exception as e:
            log.error(f"get_positions: {e}")
            return []

    async def set_leverage(self, symbol: str, leverage: int):
        if not API_KEY:
            return
        try:
            await self._post("/v5/position/set-leverage", {
                "category": "linear", "symbol": symbol,
                "buyLeverage": str(leverage), "sellLeverage": str(leverage),
            })
        except Exception as e:
            if "110043" not in str(e):   # já está nessa alavancagem
                log.warning(f"set_leverage {symbol}: {e}")

    async def place_order(self, symbol: str, side: str, qty: float,
                          sl: float = 0, tp: float = 0) -> dict:
        if not API_KEY:
            log.info(f"[DEMO] {side} {qty} {symbol} SL={sl} TP={tp}")
            return {"orderId": "demo"}
        body = {
            "category": "linear", "symbol": symbol,
            "side": side, "orderType": "Market",
            "qty": str(qty), "timeInForce": "IOC",
            "positionIdx": 0,
        }
        if sl > 0: body["stopLoss"]   = str(round(sl, 6))
        if tp > 0: body["takeProfit"] = str(round(tp, 6))
        return await self._post("/v5/order/create", body)

    async def set_sl(self, symbol: str, sl: float):
        if not API_KEY:
            return
        try:
            await self._post("/v5/position/trading-stop", {
                "category": "linear", "symbol": symbol,
                "stopLoss": str(round(sl, 6)), "positionIdx": 0,
            })
        except Exception as e:
            log.error(f"set_sl {symbol}: {e}")

    # ── WebSocket público ───────────────────────────────────────
    async def start_websocket(self, symbols: list, intervals: list = None):
        """Inicia WS em background. Reconecta automaticamente."""
        if not symbols:
            log.error("❌ start_websocket chamado com lista de símbolos vazia — abortando")
            return
        # Cancela task anterior se já concluída (ex: abortou por lista vazia)
        if self._ws_task and not self._ws_task.done():
            return
        if self._ws_task and self._ws_task.done():
            self._ws_task = None
        self._ws_task = asyncio.create_task(
            self._ws_loop(symbols, intervals or ["15", "60"])
        )

    async def _ws_loop(self, symbols: list, intervals: list):
        import websockets
        if not symbols:
            log.error("❌ WebSocket: lista de símbolos vazia — abortando subscrição")
            return

        while True:
            try:
                log.info(
                    f"🔌 WebSocket Bybit conectando... "
                    f"({len(symbols)} símbolos: {', '.join(symbols)})"
                )
                async with websockets.connect(
                    WS_PUBLIC, ping_interval=30, ping_timeout=20, close_timeout=10
                ) as ws:
                    # Subscreve tickers e klines
                    topics = []
                    for sym in symbols:
                        topics.append(f"tickers.{sym}")
                        for iv in intervals:
                            topics.append(f"kline.{iv}.{sym}")

                    if not topics:
                        log.error("❌ WebSocket: nenhum topic gerado — verifique símbolos e intervalos")
                        return

                    log.info(
                        f"📡 Subscrevendo {len(topics)} topics: "
                        f"{', '.join(topics[:6])}"
                        f"{'...' if len(topics) > 6 else ''}"
                    )

                    # Bybit limita 10 topics por mensagem
                    sub_errors = 0
                    for i in range(0, len(topics), 10):
                        batch = topics[i:i+10]
                        try:
                            await ws.send(json.dumps({
                                "op": "subscribe",
                                "args": batch,
                            }))
                        except Exception as e:
                            sub_errors += 1
                            log.error(f"WS subscribe batch {i//10 + 1}: {e}")

                    if sub_errors:
                        log.warning(f"⚠️ WebSocket: {sub_errors} batch(es) com erro de subscrição")
                    else:
                        self._ws_retry = 0   # reset backoff
                    log.info(f"✅ WebSocket subscrito: {len(topics)} topics em {(len(topics) + 9) // 10} batch(es)")

                    # Ping manual Bybit a cada 20s (mais confiável que TCP ping)
                    last_ping = asyncio.get_event_loop().time()
                    async for raw in ws:
                        now = asyncio.get_event_loop().time()
                        if now - last_ping > 20:
                            try:
                                await ws.send('{"op":"ping"}')
                                last_ping = now
                            except Exception:
                                break
                        try:
                            msg = json.loads(raw)
                            if msg.get("op") == "pong":
                                continue   # ignora pong
                            await self._handle_ws(msg)
                        except Exception as e:
                            log.error(f"WS parse: {e}")

            except Exception as e:
                log.warning(f"⚡ WebSocket desconectado: {e} — reconectando em 5s")
                await asyncio.sleep(5)

    async def _handle_ws(self, msg: dict):
        topic = msg.get("topic", "")
        data  = msg.get("data", {})
        if not topic or not data:
            return

        # Ticker + CVD update
        if topic.startswith("tickers."):
            sym = topic.split(".")[1]
            if isinstance(data, dict):
                prev = self._ticker_cache.get(sym, {})
                self._ticker_cache[sym] = data
                # Atualiza CVD com cada tick
                try:
                    from bot.market_data import update_cvd
                    cur_p  = float(data.get("lastPrice", 0))
                    prev_p = float(prev.get("lastPrice", cur_p))
                    vol    = float(data.get("volume24h", 0))
                    if cur_p > 0 and prev_p > 0:
                        update_cvd(sym, cur_p, prev_p, vol)
                except Exception:
                    pass

        # Kline
        elif topic.startswith("kline."):
            parts = topic.split(".")
            iv, sym = parts[1], parts[2]
            key = f"{sym}_{iv}"
            items = data if isinstance(data, list) else [data]
            if key not in self._kline_cache:
                self._kline_cache[key] = []
            prev_len = len(self._kline_cache[key])
            for item in items:
                k = {
                    "o": float(item.get("open",  0)),
                    "h": float(item.get("high",  0)),
                    "l": float(item.get("low",   0)),
                    "c": float(item.get("close", 0)),
                    "v": float(item.get("volume",0)),
                }
                cache = self._kline_cache[key]
                if cache and item.get("confirm") is False:
                    cache[-1] = k   # atualiza candle em formação
                else:
                    cache.append(k)
                    if len(cache) > 200:
                        cache.pop(0)
            # Log milestones so we can confirm the cache is being populated
            new_len = len(self._kline_cache[key])
            if prev_len == 0 and new_len > 0:
                log.info(f"📦 WS cache iniciado: {key} ({new_len} candles)")
            elif prev_len < 20 <= new_len:
                log.info(f"📦 WS cache pronto: {key} ({new_len} candles) — fast-track ativo")

    def get_cached_klines(self, symbol: str, interval: str, limit: int = 100) -> list:
        """Retorna klines do cache WS. Fallback para None se não tiver."""
        key = f"{symbol}_{interval}"
        data = self._kline_cache.get(key, [])
        return data[-limit:] if data else []

    def get_cached_ticker(self, symbol: str) -> dict:
        return self._ticker_cache.get(symbol, {})

    def get_cache_stats(self) -> dict:
        """Returns a summary of the current WS kline cache state for diagnostics."""
        stats = {}
        for key, candles in self._kline_cache.items():
            stats[key] = len(candles)
        total_keys   = len(stats)
        ready_keys   = sum(1 for v in stats.values() if v >= 20)
        total_candles = sum(stats.values())
        log.info(
            f"📊 WS cache: {ready_keys}/{total_keys} keys prontos "
            f"({total_candles} candles total)"
        )
        return {
            "keys":          total_keys,
            "ready":         ready_keys,
            "total_candles": total_candles,
            "detail":        stats,
        }
