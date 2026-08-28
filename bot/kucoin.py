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
API_KEY        = os.environ.get("KUCOIN_API_KEY",        "")
API_SECRET     = os.environ.get("KUCOIN_API_SECRET",     "")
API_PASSPHRASE = os.environ.get("KUCOIN_API_PASSPHRASE", "")
# PAPER_TRADE FORÇADO TEMPORARIAMENTE — protege capital durante correção de auth
# Remover esta linha e descomentar a próxima quando autenticação estiver OK
PAPER_TRADE    = True
# PAPER_TRADE    = os.environ.get("PAPER_TRADE", "false").lower() == "true"

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


class KuCoinClient:
    """
    Cliente KuCoin Futures com interface idêntica ao BybitClient.
    Substitui bot/bybit.py sem alterar engine.py, strategy.py ou qualquer
    outro módulo — apenas a variável de import muda.
    """

    def __init__(self):
        self._session:   Optional[aiohttp.ClientSession] = None
        self._ws_retry:  int  = 0
        self._ws_token:  str  = ""
        self._ws_token_ts: float = 0.0   # timestamp da obtenção do token WS

        # Cache de klines e tickers (igual ao BybitClient)
        self._kline_cache: dict = {}    # (symbol, interval) → deque[dict]
        self._ticker_cache: dict = {}   # symbol → dict
        self._ob_cache:    dict = {}    # symbol → dict
        self._instruments: dict = {}    # symbol → {minQty, qtyStep, tickSize, minNotional}

        self._connected = False
        self._running   = False

    # ── Sessão HTTP ───────────────────────────────────────────────
    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )

    # ── Autenticação HMAC-SHA256 ──────────────────────────────────
    def _sign(self, timestamp: str, method: str, endpoint: str, body: str = "") -> str:
        """
        KuCoin signature: HMAC-SHA256( timestamp + method + endpoint + body )
        Retorna base64 do digest.
        """
        message = f"{timestamp}{method.upper()}{endpoint}{body}"
        sig = hmac.new(
            API_SECRET.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return b64encode(sig).decode()

    def _sign_passphrase(self) -> str:
        """KuCoin exige passphrase também assinada com HMAC-SHA256."""
        sig = hmac.new(
            API_SECRET.encode("utf-8"),
            API_PASSPHRASE.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return b64encode(sig).decode()

    def _auth_headers(self, method: str, endpoint: str, body: str = "") -> dict:
        ts = str(int(time.time() * 1000))
        return {
            "KC-API-KEY":         API_KEY,
            "KC-API-SIGN":        self._sign(ts, method, endpoint, body),
            "KC-API-TIMESTAMP":   ts,
            "KC-API-PASSPHRASE":  self._sign_passphrase(),
            "KC-API-KEY-VERSION": "2",
            "Content-Type":       "application/json",
        }

    # ── Requests base ─────────────────────────────────────────────
    async def _get(self, endpoint: str, params: dict = None, auth: bool = False) -> dict:
        await self._ensure_session()
        # CORRIGIDO: KuCoin exige que a assinatura de GET inclua a query string
        # Exemplo: assina "/api/v1/account-overview?currency=USDT" (não só o path)
        if params:
            from urllib.parse import urlencode
            query_string = urlencode(params)
            signed_endpoint = f"{endpoint}?{query_string}"
        else:
            signed_endpoint = endpoint
        url     = REST_BASE + endpoint
        headers = self._auth_headers("GET", signed_endpoint) if auth else {}
        for attempt in range(3):
            try:
                async with self._session.get(url, params=params, headers=headers) as r:
                    data = await r.json()
                    if data.get("code") == "200000":
                        return data.get("data", {})
                    log.warning(f"KuCoin GET {endpoint}: {data.get('code')} {data.get('msg','')}")
                    return {}
            except Exception as e:
                wait = 2 ** attempt
                log.warning(f"KuCoin GET {endpoint} tentativa {attempt+1}: {e} — retry em {wait}s")
                await asyncio.sleep(wait)
        return {}

    async def _post(self, endpoint: str, body: dict) -> dict:
        await self._ensure_session()
        url      = REST_BASE + endpoint
        body_str = json.dumps(body)
        headers  = self._auth_headers("POST", endpoint, body_str)
        for attempt in range(3):
            try:
                async with self._session.post(url, data=body_str, headers=headers) as r:
                    data = await r.json()
                    if data.get("code") == "200000":
                        return data.get("data", {})
                    # Erros não-recuperáveis
                    msg = data.get("msg", "")
                    code = data.get("code", "")
                    if code in ("400100", "300004", "200004"):  # param inválido, saldo insuf.
                        log.error(f"KuCoin POST {endpoint} erro permanente {code}: {msg}")
                        return {}
                    log.warning(f"KuCoin POST {endpoint}: {code} {msg}")
            except Exception as e:
                wait = 2 ** attempt
                log.warning(f"KuCoin POST {endpoint} tentativa {attempt+1}: {e} — retry em {wait}s")
                await asyncio.sleep(wait)
        return {}

    # ── Saldo ─────────────────────────────────────────────────────
    async def get_balance(self) -> float:
        """Retorna saldo disponível em USDT na conta de futuros."""
        data = await self._get("/api/v1/account-overview", {"currency": "USDT"}, auth=True)
        bal = float(data.get("availableBalance", 0))
        log.info(f"💰 Saldo USDT: ${bal:.4f}")
        return bal

    # ── Instrumentos ──────────────────────────────────────────────
    async def load_instruments(self):
        """
        Carrega especificações dos instrumentos KuCoin Futures.
        CORRIGIDO: endpoint público não requer auth; resposta é lista direta.
        Campos KuCoin: lotSize (tamanho mínimo), tickSize, multiplier (USDT/contrato).
        """
        data = await self._get("/api/v1/contracts/active")  # endpoint público, sem auth
        # KuCoin retorna: {"code":"200000","data":[{...}, ...]}
        # Após _get(), data já é o valor de "data" — pode ser lista ou dict
        if isinstance(data, list):
            contracts = data
        elif isinstance(data, dict):
            contracts = data.get("dataList", data.get("items", []))
        else:
            contracts = []

        for c in contracts:
            kc_sym   = c.get("symbol", "")
            std_sym  = to_standard(kc_sym)
            if not std_sym or std_sym == kc_sym:
                continue   # pula símbolos fora do nosso mapa
            lot_size = float(c.get("lotSize",    1.0))
            tick_sz  = float(c.get("tickSize",   0.01))
            mult     = float(c.get("multiplier", 0.001))  # BTC: 0.001; USDT-margined varia
            self._instruments[std_sym] = {
                "minQty":      lot_size,
                "qtyStep":     lot_size,
                "tickSize":    tick_sz,
                "multiplier":  mult,
                "minNotional": lot_size * mult * 100,  # estimativa conservadora
            }
        log.info(f"📋 {len(self._instruments)} instrumentos carregados da KuCoin")
        return self._instruments

    def get_instruments(self) -> dict:
        return self._instruments

    # ── Alavancagem ───────────────────────────────────────────────
    async def set_leverage(self, symbol: str, leverage: int):
        """
        Define alavancagem para um par.
        KuCoin Futures: POST /api/v2/changeCrossUserLeverage (cross margin)
        ou via parâmetro leverage diretamente na ordem (mais simples).
        CORRIGIDO: endpoint correto para KuCoin Futures v1.
        """
        kc_sym = to_kucoin(symbol)
        # KuCoin Futures: leverage é definido por posição (isolada) ou conta (cross)
        # A forma mais confiável é passar leverage na própria ordem place_order
        # Este endpoint é para garantia — pode não existir em todas as versões da API
        try:
            await self._post("/api/v1/position/margin/auto-deposit-status", {
                "symbol": kc_sym,
                "status": False,  # isolated margin
            })
        except Exception:
            pass  # ignora se não suportado — leverage é passado na ordem
        log.info(f"⚙️ Leverage {symbol}: {leverage}x (aplicado via parâmetro da ordem)")

    # ── Ordens ────────────────────────────────────────────────────
    def _round_price(self, price: float, symbol: str) -> str:
        """Arredonda preço ao tickSize correto do instrumento."""
        info      = self._instruments.get(symbol, {})
        tick_size = float(info.get("tickSize", 0.01))
        ticks     = round(price / tick_size)
        clean     = round(ticks * tick_size, 8)
        decimals  = max(0, -int(math.floor(math.log10(tick_size)))) if tick_size < 1 else 0
        return f"{clean:.{decimals}f}"

    def _round_qty(self, qty: float, symbol: str) -> int:
        """
        KuCoin Futures usa CONTRATOS (inteiros), não quantidade base.
        qty (USDT-base) → contratos = round(qty / multiplier)
        """
        info       = self._instruments.get(symbol, {})
        multiplier = float(info.get("multiplier", 1.0))
        lot_size   = float(info.get("minQty",     1.0))
        contracts  = max(1, round(qty / multiplier / lot_size)) * int(lot_size)
        return max(1, int(contracts))

    async def place_order(self, symbol: str, side: str, qty: float,
                          sl: float = 0, tp: float = 0,
                          instruments: dict = None) -> dict:
        """
        Envia ordem a mercado com SL e TP opcionais.
        side: "Buy" ou "Sell" (mesmo padrão do BybitClient)
        qty: quantidade em unidade base (convertida para contratos internamente)
        """
        if PAPER_TRADE:
            log.info(f"[PAPER] {side} {qty} {symbol} SL={sl} TP={tp}")
            return {"orderId": f"paper_{int(time.time()*1000)}"}

        if not API_KEY:
            log.warning("place_order: KUCOIN_API_KEY não configurado")
            return {}

        kc_sym    = to_kucoin(symbol)
        contracts = self._round_qty(qty, symbol)

        # KuCoin: "buy" = long, "sell" = short (lowercase)
        kc_side   = side.lower()

        # clientOid: idempotency key (máx 40 chars alfanumérico)
        _ts      = str(int(time.time() * 1000))
        _raw     = f"{symbol}_{side}_{contracts}_{_ts}"
        _oid     = hashlib.md5(_raw.encode()).hexdigest()[:40]

        body: dict = {
            "clientOid": _oid,
            "symbol":    kc_sym,
            "side":      kc_side,
            "type":      "market",
            "size":      str(contracts),
            "leverage":  str(os.environ.get("LEVERAGE", "10")),
        }

        # SL server-side
        if sl > 0:
            body["stop"]          = "down" if side == "Buy" else "up"
            body["stopPrice"]     = self._round_price(sl, symbol)
            body["stopPriceType"] = "TP"   # mark price

        # TP server-side
        if tp > 0:
            # KuCoin Futures v2: takeProfit separado
            body["takeProfit"] = self._round_price(tp, symbol)

        data = await self._post("/api/v1/orders", body)
        order_id = data.get("orderId", "")
        log.info(f"📤 Ordem {side} {contracts} contratos {symbol} → orderId={order_id}")
        return data

    # ── Trailing Stop / Set SL ────────────────────────────────────
    async def set_sl(self, symbol: str, sl: float, instruments: dict = None) -> bool:
        """
        Atualiza Stop Loss de uma posição aberta.
        KuCoin: POST /api/v1/position/trading-stop
        SL arredondado ao tickSize correto (bug corrigido v12).
        """
        if not API_KEY:
            return False
        kc_sym    = to_kucoin(symbol)
        sl_str    = self._round_price(sl, symbol)
        data      = await self._post("/api/v1/position/trading-stop", {
            "symbol":   kc_sym,
            "stopLoss": sl_str,
        })
        log.debug(f"set_sl {symbol}: {sl:.6f} → '{sl_str}'")
        return bool(data)

    # ── Cancelar ordens ───────────────────────────────────────────
    async def cancel_all_orders(self, symbol: str = "") -> bool:
        """
        Cancela todas as ordens abertas.
        KuCoin Futures: DELETE /api/v1/orders
        CORRIGIDO: removido código duplicado (POST + DELETE ao mesmo tempo).
        """
        await self._ensure_session()
        kc_sym   = to_kucoin(symbol) if symbol else ""
        params   = {"symbol": kc_sym} if kc_sym else {}
        endpoint = "/api/v1/orders"
        # Monta query string para incluir na assinatura (mesmo padrão do _get corrigido)
        if params:
            from urllib.parse import urlencode
            signed_endpoint = f"{endpoint}?{urlencode(params)}"
        else:
            signed_endpoint = endpoint
        headers = self._auth_headers("DELETE", signed_endpoint)
        url     = REST_BASE + endpoint
        try:
            async with self._session.delete(url, params=params, headers=headers) as r:
                d = await r.json()
                ok = d.get("code") == "200000"
                if ok:
                    log.info(f"✓ cancel_all_orders {symbol or 'todos'}")
                else:
                    log.warning(f"cancel_all_orders: {d.get('code')} {d.get('msg','')}")
                return ok
        except Exception as e:
            log.error(f"cancel_all_orders {symbol}: {e}")
            return False

    # ── Klines / Candles ──────────────────────────────────────────
    async def get_klines(self, symbol: str, interval: str, limit: int = 200) -> list:
        """
        Busca klines históricas.
        KuCoin endpoint: GET /api/v1/kline/query
        interval: mesmo formato Bybit (15, 60, 240) — convertido internamente
        """
        kc_sym      = to_kucoin(symbol)
        gran        = INTERVAL_MAP.get(str(interval), int(interval))   # granularidade em minutos
        gran_sec    = gran * 60
        end_ts      = int(time.time() * 1000)
        start_ts    = end_ts - gran_sec * limit * 1000

        # KuCoin Futures kline endpoint correto
        # granularity em minutos: 1, 5, 15, 30, 60, 120, 240, 480, 720, 1440, 10080
        data = await self._get("/api/v1/kline/query", {
            "symbol":      kc_sym,
            "granularity": str(gran),
            "from":        str(start_ts // 1000),  # KuCoin aceita segundos, não ms
            "to":          str(end_ts   // 1000),
        })

        klines = []
        raw    = data if isinstance(data, list) else []
        for k in raw:
            # KuCoin Futures kline: [timestamp_ms, open, high, low, close, volume, turnover]
            # Nota: KuCoin retorna do mais recente para o mais antigo — inverter no final
            try:
                klines.append({
                    "ts": int(float(k[0])) * (1 if int(float(k[0])) > 1e10 else 1000),
                    "o":  float(k[1]),
                    "h":  float(k[2]),
                    "l":  float(k[3]),
                    "c":  float(k[4]),
                    "v":  float(k[5]),
                })
            except (IndexError, ValueError, TypeError):
                continue
        klines = list(reversed(klines))  # mais antigo primeiro (padrão do engine)

        # Atualiza cache
        key = (symbol, str(interval))
        if key not in self._kline_cache:
            self._kline_cache[key] = deque(maxlen=500)
        self._kline_cache[key].extend(klines[-limit:])
        return klines[-limit:]

    def get_cached_klines(self, symbol: str, interval: str, limit: int = 200) -> list:
        key = (symbol, str(interval))
        cached = list(self._kline_cache.get(key, []))
        return cached[-limit:] if cached else []

    # ── Ticker ────────────────────────────────────────────────────
    async def get_ticker(self, symbol: str) -> dict:
        kc_sym = to_kucoin(symbol)
        data   = await self._get("/api/v1/ticker", {"symbol": kc_sym})
        ticker = {
            "symbol":    symbol,
            "lastPrice": float(data.get("price",      0)),
            "bid":       float(data.get("bestBidPrice", 0)),
            "ask":       float(data.get("bestAskPrice", 0)),
            "volume":    float(data.get("volume",     0)),
            "turnover":  float(data.get("turnover",   0)),
        }
        self._ticker_cache[symbol] = ticker
        return ticker

    def get_cached_ticker(self, symbol: str) -> dict:
        return self._ticker_cache.get(symbol, {})

    # ── Open Interest ─────────────────────────────────────────────
    async def get_open_interest(self, symbol: str) -> dict:
        kc_sym = to_kucoin(symbol)
        data   = await self._get("/api/v1/contracts/" + kc_sym)
        return {
            "openInterest":       str(data.get("openInterest",     0)),
            "openInterestValue":  str(data.get("openInterestValue", 0)),
        }

    # ── Funding Rate ──────────────────────────────────────────────
    async def get_funding_rate(self, symbol: str) -> float:
        kc_sym = to_kucoin(symbol)
        data   = await self._get("/api/v1/funding-rate/" + kc_sym + "/current")
        return float(data.get("value", 0))

    # ── Orderbook ─────────────────────────────────────────────────
    async def get_orderbook(self, symbol: str, depth: int = 20) -> dict:
        kc_sym = to_kucoin(symbol)
        data   = await self._get("/api/v1/level2/depth20", {"symbol": kc_sym})
        ob = {
            "b": [[str(b[0]), str(b[1])] for b in data.get("bids", [])[:depth]],
            "a": [[str(a[0]), str(a[1])] for a in data.get("asks", [])[:depth]],
        }
        self._ob_cache[symbol] = ob
        return ob

    # ── Posições abertas ──────────────────────────────────────────
    async def get_positions(self) -> list:
        """Retorna posições abertas formatadas igual ao BybitClient."""
        data = await self._get("/api/v1/positions", auth=True)
        positions = []
        raw = data if isinstance(data, list) else []
        for p in raw:
            qty = float(p.get("currentQty", 0))
            if qty == 0:
                continue
            kc_sym = p.get("symbol", "")
            positions.append({
                "symbol":       to_standard(kc_sym),
                "side":         "Buy" if qty > 0 else "Sell",
                "size":         abs(qty),
                "entryPrice":   float(p.get("avgEntryPrice",  0)),
                "markPrice":    float(p.get("markPrice",      0)),
                "unrealisedPnl": float(p.get("unrealisedPnl", 0)),
                "leverage":     float(p.get("realLeverage",   1)),
            })
        return positions

    # ── WebSocket ─────────────────────────────────────────────────
    async def _get_ws_token(self) -> str:
        """
        KuCoin WS requer token obtido via REST (válido 18h).
        POST /api/v1/bullet-private → retorna token + endpoint WS.
        """
        now = time.time()
        if self._ws_token and (now - self._ws_token_ts) < 60000:
            return self._ws_token   # reusa token válido

        await self._ensure_session()
        endpoint = "/api/v1/bullet-private"
        headers  = self._auth_headers("POST", endpoint, "")
        url      = REST_BASE + endpoint
        try:
            async with self._session.post(url, headers=headers) as r:
                data = (await r.json()).get("data", {})
            self._ws_token    = data.get("token", "")
            self._ws_token_ts = now
            log.info("✓ WS token KuCoin obtido")
            return self._ws_token
        except Exception as e:
            log.error(f"_get_ws_token: {e}")
            return ""

    async def start_websocket(self, symbols: list, intervals: list = None):
        """Inicia WebSocket para klines e tickers de todos os símbolos."""
        if not symbols:
            log.error("start_websocket: lista de símbolos vazia")
            return
        asyncio.create_task(self._ws_loop(symbols, intervals or ["15", "60"]))
        log.info(f"🔌 WebSocket KuCoin iniciando para {len(symbols)} símbolos")

    async def _ws_loop(self, symbols: list, intervals: list):
        """Loop principal de reconexão WebSocket com backoff exponencial."""
        import websockets

        while True:
            try:
                token = await self._get_ws_token()
                if not token:
                    await asyncio.sleep(10)
                    continue

                ws_url = f"wss://ws-api.kucoin.com/endpoint?token={token}&connectId=bgx"

                async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                    self._ws_retry = 0
                    self._connected = True
                    log.info(f"✅ WebSocket KuCoin conectado")

                    # Subscrever klines por símbolo e intervalo (batches de 10)
                    subs = []
                    for sym in symbols:
                        kc_sym = to_kucoin(sym)
                        for interval in intervals:
                            gran = INTERVAL_MAP.get(str(interval), int(interval))
                            subs.append(f"/contractMarket/candle:{kc_sym}_{gran}")
                        subs.append(f"/contractMarket/ticker:{kc_sym}")

                    # KuCoin suporta múltiplos tópicos por mensagem
                    for i in range(0, len(subs), 10):
                        batch = subs[i:i+10]
                        await ws.send(json.dumps({
                            "id":             str(int(time.time() * 1000)),
                            "type":           "subscribe",
                            "topic":          ",".join(batch),
                            "privateChannel": False,
                            "response":       True,
                        }))

                    # Ping manual a cada 20s (KuCoin exige)
                    async def _ping():
                        while True:
                            await asyncio.sleep(20)
                            try:
                                await ws.send(json.dumps({
                                    "id":   str(int(time.time() * 1000)),
                                    "type": "ping",
                                }))
                            except Exception:
                                break

                    asyncio.create_task(_ping())

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            await self._handle_ws_message(msg)
                        except Exception as e:
                            log.debug(f"WS parse: {e}")

            except Exception as e:
                self._ws_retry += 1
                wait = min(2 ** self._ws_retry, 60)
                log.warning(
                    f"⚡ WebSocket KuCoin desconectado: {e} "
                    f"— tentativa #{self._ws_retry}, reconectando em {wait}s"
                )
                self._connected = False
                await asyncio.sleep(wait)

    async def _handle_ws_message(self, msg: dict):
        """Processa mensagens WebSocket e atualiza caches."""
        topic  = msg.get("topic", "")
        data   = msg.get("data", {})
        m_type = msg.get("type", "")

        if m_type in ("welcome", "ack", "pong"):
            return

        # ── Candles / Klines ──────────────────────────────────────
        if "candle" in topic and "candles" in data:
            # topic: /contractMarket/candle:XBTUSDTM_15
            parts     = topic.split(":")[-1].split("_")
            kc_sym    = "_".join(parts[:-1])
            gran_str  = parts[-1]
            std_sym   = to_standard(kc_sym)

            # Mapear granularidade → interval string (Bybit style)
            rev_map = {str(v): k for k, v in INTERVAL_MAP.items()}
            interval = rev_map.get(gran_str, gran_str)

            candles = data["candles"]
            # KuCoin retorna: [timestamp, open, close, high, low, volume, turnover]
            kline = {
                "ts": int(candles[0]),
                "o":  float(candles[1]),
                "c":  float(candles[2]),
                "h":  float(candles[3]),
                "l":  float(candles[4]),
                "v":  float(candles[5]),
            }
            key = (std_sym, str(interval))
            if key not in self._kline_cache:
                self._kline_cache[key] = deque(maxlen=500)
            cache = self._kline_cache[key]
            if cache and cache[-1]["ts"] == kline["ts"]:
                cache[-1] = kline   # atualiza candle atual
            else:
                cache.append(kline)

        # ── Ticker ────────────────────────────────────────────────
        elif "ticker" in topic:
            kc_sym  = topic.split(":")[-1]
            std_sym = to_standard(kc_sym)
            self._ticker_cache[std_sym] = {
                "symbol":    std_sym,
                "lastPrice": float(data.get("price",        0)),
                "bid":       float(data.get("bestBidPrice", 0)),
                "ask":       float(data.get("bestAskPrice", 0)),
                "volume":    float(data.get("size",         0)),
            }

    # ── Cache stats ───────────────────────────────────────────────
    def get_cache_stats(self) -> dict:
        return {
            "kline_pairs": len(self._kline_cache),
            "tickers":     len(self._ticker_cache),
            "instruments": len(self._instruments),
            "ws_connected": self._connected,
            "ws_retries":   self._ws_retry,
        }

    # ── Encerramento ─────────────────────────────────────────────
    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        log.info("👋 KuCoinClient encerrado")
