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
# .strip() automático — espaços acidentais no Railway são a causa #1 de 400004
API_KEY        = os.environ.get("KUCOIN_API_KEY",        "").strip()
API_SECRET     = os.environ.get("KUCOIN_API_SECRET",     "").strip()
API_PASSPHRASE = os.environ.get("KUCOIN_API_PASSPHRASE", "").strip()
# OPERAÇÃO REAL ATIVADA — autenticação KuCoin confirmada (saldo real lido com sucesso).
# Controlado pela env var PAPER_TRADE no Railway:
#   PAPER_TRADE=true  → simula ordens (nada é executado na exchange)
#   PAPER_TRADE=false → executa ordens REAIS com capital real (padrão)
PAPER_TRADE    = os.environ.get("PAPER_TRADE", "false").lower() == "true"

# ── Endpoints ─────────────────────────────────────────────────────
REST_BASE = "https://api-futures.kucoin.com"
WS_BASE   = "wss://ws-api.kucoin.com/endpoint"

# ── Constantes ────────────────────────────────────────────────────
# Taxa taker KuCoin Futures: 0.06% (auditoria #8 — Bybit era 0.055%)
# Configurável caso sua conta tenha tier de taxa diferente.
TAKER_FEE = float(os.environ.get("TAKER_FEE", "0.0006"))
MAKER_FEE = float(os.environ.get("MAKER_FEE", "0.0002"))

# ── Mapa de símbolos Bybit → KuCoin ──────────────────────────────
# KuCoin Futures USDT-margined usa sufixo "M" (ex: XBTUSDTM, ETHUSDTM)
# ── Mapa de símbolos: padrão (BTCUSDT) ↔ KuCoin (XBTUSDTM) ──────
#
# CORRIGIDO: antes era hardcoded e continha símbolos que NÃO EXISTEM
# na KuCoin Futures (ex: BNBUSDTM). Símbolos inexistentes faziam a API
# retornar erro e o par era silenciosamente ignorado — por isso apenas
# alguns pares (como SOL) apareciam nos logs.
#
# Agora o mapa é construído DINAMICAMENTE em load_instruments() a partir
# de /api/v1/contracts/active, garantindo que só existam pares reais.
#
# Exceções conhecidas de nomenclatura KuCoin:
#   BTC  → XBT   (KuCoin usa o código XBT para Bitcoin)
_BASE_ALIAS = {
    "BTC": "XBT",   # KuCoin nomeia Bitcoin como XBT
}
_BASE_ALIAS_REV = {v: k for k, v in _BASE_ALIAS.items()}

# Preenchidos dinamicamente por KuCoinClient.load_instruments()
SYMBOL_MAP: dict     = {}
SYMBOL_MAP_REV: dict = {}


def _guess_kucoin(symbol: str) -> str:
    """
    Converte BTCUSDT → XBTUSDTM sem consultar a API.
    Usado apenas como fallback antes do mapa dinâmico ser carregado.
    """
    if not symbol.endswith("USDT"):
        return symbol
    base = symbol[:-4]                      # BTCUSDT → BTC
    base = _BASE_ALIAS.get(base, base)      # BTC → XBT
    return f"{base}USDTM"


def to_kucoin(symbol: str) -> str:
    """Converte símbolo padrão (BTCUSDT) → KuCoin (XBTUSDTM)."""
    if symbol in SYMBOL_MAP:
        return SYMBOL_MAP[symbol]
    return _guess_kucoin(symbol)


def to_standard(symbol: str) -> str:
    """Converte símbolo KuCoin (XBTUSDTM) → padrão (BTCUSDT)."""
    if symbol in SYMBOL_MAP_REV:
        return SYMBOL_MAP_REV[symbol]
    if symbol.endswith("USDTM"):
        base = symbol[:-5]                       # XBTUSDTM → XBT
        base = _BASE_ALIAS_REV.get(base, base)   # XBT → BTC
        return f"{base}USDT"
    return symbol

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
        # Versão da API Key: "2" = passphrase assinada | "1" = texto plano
        # Fallback automático para "1" se receber 400004 com "2"
        self._api_version: str = os.environ.get("KUCOIN_API_VERSION", "2")
        self._version_fallback_done: bool = False
        # Offset entre relógio local e servidor KuCoin (ms).
        # KuCoin rejeita requests com timestamp fora de ±5s → 400005 Invalid SIGN.
        self._time_offset_ms: int = 0
        self._time_synced: bool = False
        self._sign_logged: bool = False   # loga a primeira assinatura para diagnóstico

        # Diagnóstico de credenciais no startup
        # Aviso explícito do modo de operação
        if PAPER_TRADE:
            log.warning("🟡 PAPER TRADE — ordens simuladas, capital NÃO é usado")
        else:
            log.warning("🔴 OPERAÇÃO REAL ATIVA — ordens serão executadas com CAPITAL REAL")

        if API_KEY:
            log.info(f"🔑 KuCoin API Key: {API_KEY[:6]}...{API_KEY[-4:]} ({len(API_KEY)} chars)")
            log.info(
                f"🔑 API Secret: {len(API_SECRET)} chars | "
                f"Passphrase: {len(API_PASSPHRASE)} chars | "
                f"API version: v{self._api_version}"
            )
            if not API_PASSPHRASE:
                log.error("❌ KUCOIN_API_PASSPHRASE VAZIA — configure no Railway!")
            if not API_SECRET:
                log.error("❌ KUCOIN_API_SECRET VAZIA — configure no Railway!")
            # Detecta espaços acidentais (causa comum de 400004)
            if API_PASSPHRASE != API_PASSPHRASE.strip():
                log.error("❌ Passphrase tem ESPAÇOS no início/fim — remova no Railway!")
            if API_SECRET != API_SECRET.strip():
                log.error("❌ API Secret tem ESPAÇOS no início/fim — remova no Railway!")
            # Alertar sobre caracteres problemáticos na passphrase
            import string
            special = [c for c in API_PASSPHRASE if c not in string.ascii_letters + string.digits + "_-!@#$%"]
            if special:
                log.warning(f"⚠️  Passphrase contém chars especiais: {special} — pode causar 400004")
        else:
            log.warning("⚠️  KUCOIN_API_KEY não configurado")

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

        Formato exato exigido pela KuCoin:
          str_to_sign = timestamp + METHOD + /api/v1/endpoint?query + body
        """
        message = f"{timestamp}{method.upper()}{endpoint}{body}"
        sig = hmac.new(
            API_SECRET.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        signature = b64encode(sig).decode()
        # Log de diagnóstico — mostra o que está sendo assinado (sem expor o secret)
        if not self._sign_logged:
            self._sign_logged = True
            log.info(f"🔏 Assinando: '{message[:80]}...' → sign[:12]={signature[:12]}")
        return signature

    def _sign_passphrase(self) -> str:
        """KuCoin exige passphrase também assinada com HMAC-SHA256."""
        sig = hmac.new(
            API_SECRET.encode("utf-8"),
            API_PASSPHRASE.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return b64encode(sig).decode()

    def _auth_headers(self, method: str, endpoint: str, body: str = "") -> dict:
        """
        Headers de autenticação KuCoin.

        IMPORTANTE — v1 vs v2:
          API Key v2: passphrase ASSINADA com HMAC-SHA256 (base64)
          API Key v1: passphrase em TEXTO PLANO

        Enviar passphrase assinada para uma key v1 (ou vice-versa) resulta
        em "400004 Invalid KC-API-PASSPHRASE".

        self._api_version controla qual formato usar. Começa em "2" e faz
        fallback automático para "1" se receber 400004.
        """
        ts = str(self._now_ms())   # timestamp ajustado pelo offset do servidor
        if self._api_version == "2":
            passphrase = self._sign_passphrase()   # assinada
        else:
            passphrase = API_PASSPHRASE            # texto plano (v1)
        return {
            "KC-API-KEY":         API_KEY,
            "KC-API-SIGN":        self._sign(ts, method, endpoint, body),
            "KC-API-TIMESTAMP":   ts,
            "KC-API-PASSPHRASE":  passphrase,
            "KC-API-KEY-VERSION": self._api_version,
            "Content-Type":       "application/json",
        }

    # ── Requests base ─────────────────────────────────────────────
    async def _get(self, endpoint: str, params: dict = None, auth: bool = False) -> dict:
        await self._ensure_session()
        from urllib.parse import urlencode

        # Para autenticação KuCoin: a URL assinada DEVE ser idêntica à URL enviada.
        # Problema anterior: aiohttp montava a URL com params internamente podendo
        # reordenar os parâmetros, causando mismatch com a assinatura.
        # SOLUÇÃO: montar a URL completa manualmente e enviar sem params separados.
        if params:
            query_string   = urlencode(params)
            signed_endpoint = f"{endpoint}?{query_string}"
            full_url        = REST_BASE + signed_endpoint  # URL idêntica à assinada
        else:
            signed_endpoint = endpoint
            full_url        = REST_BASE + endpoint

        headers = self._auth_headers("GET", signed_endpoint) if auth else {}

        for attempt in range(3):
            try:
                # params=None — URL já contém os params embutidos (sem risco de reordenação)
                async with self._session.get(full_url, headers=headers) as r:
                    data = await r.json()
                    if data.get("code") == "200000":
                        return data.get("data", {})

                    code = data.get("code", "")

                    # Erros de autenticação: 400004 (passphrase) / 400005 (sign)
                    # Estratégia: alterna v2↔v1 e re-sincroniza o relógio.
                    # NÃO trava numa versão — cada erro tenta a alternativa.
                    if code in ("400004", "400005") and attempt < 2:
                        old_ver = self._api_version
                        self._api_version = "1" if old_ver == "2" else "2"

                        # 400005 = sign inválido → pode ser drift de relógio
                        if code == "400005" and not self._time_synced:
                            await self.sync_time()

                        log.warning(
                            f"🔄 {code} com API v{old_ver} → tentando v{self._api_version} "
                            f"(passphrase {'texto plano' if self._api_version == '1' else 'assinada'})"
                        )
                        headers = self._auth_headers("GET", signed_endpoint)
                        continue   # retenta com a outra versão

                    log.warning(f"KuCoin GET {endpoint}: {code} {data.get('msg','')}")
                    return {}
            except Exception as e:
                wait = 2 ** attempt
                log.warning(f"KuCoin GET {endpoint} tentativa {attempt+1}: {e} — retry em {wait}s")
                await asyncio.sleep(wait)
        return {}

    async def _post(self, endpoint: str, body: dict) -> dict:
        await self._ensure_session()
        url = REST_BASE + endpoint
        # separators=(",", ":") remove espaços — garante body idêntico entre assinatura e envio
        body_str = json.dumps(body, separators=(",", ":"))
        headers  = self._auth_headers("POST", endpoint, body_str)
        for attempt in range(3):
            try:
                async with self._session.post(url, data=body_str, headers=headers) as r:
                    data = await r.json()
                    if data.get("code") == "200000":
                        return data.get("data", {})
                    msg  = data.get("msg", "")
                    code = data.get("code", "")

                    # Erros de autenticação: alterna v2↔v1 + re-sincroniza relógio
                    if code in ("400004", "400005") and attempt < 2:
                        old_ver = self._api_version
                        self._api_version = "1" if old_ver == "2" else "2"
                        if code == "400005" and not self._time_synced:
                            await self.sync_time()
                        log.warning(
                            f"🔄 {code} com API v{old_ver} → tentando v{self._api_version}"
                        )
                        headers = self._auth_headers("POST", endpoint, body_str)
                        continue

                    if code in ("400100", "300004", "200004"):
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
        Carrega contratos ativos da KuCoin e CONSTRÓI O MAPA DE SÍMBOLOS.

        CORRIGIDO: antes usava um mapa hardcoded com símbolos inventados
        (ex: BNBUSDTM não existe na KuCoin). Pares inexistentes falhavam
        silenciosamente, e só os que por acaso batiam apareciam nos logs.

        Agora: descobre os símbolos reais via /api/v1/contracts/active e
        casa cada par de cfg.SYMBOLS com o contrato correspondente pela
        baseCurrency, respeitando aliases (BTC↔XBT).
        """
        # Sincroniza relógio ANTES de qualquer chamada autenticada
        if not self._time_synced:
            await self.sync_time()

        data = await self._get("/api/v1/contracts/active")  # público, sem auth
        if isinstance(data, list):
            contracts = data
        elif isinstance(data, dict):
            contracts = data.get("dataList", data.get("items", []))
        else:
            contracts = []

        if not contracts:
            log.error("❌ Nenhum contrato retornado pela KuCoin")
            return self._instruments

        # Indexa contratos USDT-margined pela baseCurrency
        by_base = {}
        for c in contracts:
            sym = c.get("symbol", "")
            if not sym.endswith("USDTM"):
                continue
            if c.get("status", "Open") not in ("Open", "open", None):
                continue
            base = (c.get("baseCurrency") or sym[:-5]).upper()
            by_base[base] = c

        from bot.config import cfg as _cfg
        wanted    = list(getattr(_cfg, "SYMBOLS", []))
        matched   = []
        unmatched = []

        for std_sym in wanted:
            if not std_sym.endswith("USDT"):
                unmatched.append(std_sym)
                continue
            base = std_sym[:-4].upper()             # BTCUSDT → BTC
            kc_base = _BASE_ALIAS.get(base, base)   # BTC → XBT

            c = by_base.get(kc_base) or by_base.get(base)
            if not c:
                unmatched.append(std_sym)
                continue

            kc_sym = c["symbol"]
            # Registra nos mapas globais (usados por to_kucoin/to_standard)
            SYMBOL_MAP[std_sym]      = kc_sym
            SYMBOL_MAP_REV[kc_sym]   = std_sym

            lot_size = float(c.get("lotSize",    1))
            tick_sz  = float(c.get("tickSize",   0.01))
            mult     = float(c.get("multiplier", 0.001))
            max_lev  = float(c.get("maxLeverage", 0) or 0)

            self._instruments[std_sym] = {
                "minQty":      lot_size,
                "qtyStep":     lot_size,
                "tickSize":    tick_sz,
                "multiplier":  mult,
                "maxLeverage": max_lev,
                "minNotional": lot_size * mult,
                "kucoinSymbol": kc_sym,
            }
            matched.append(f"{std_sym}→{kc_sym}")

        log.info(f"📋 {len(matched)} pares mapeados na KuCoin: {', '.join(matched)}")
        if unmatched:
            log.warning(
                f"⚠️ {len(unmatched)} pares NÃO existem na KuCoin Futures "
                f"e serão ignorados: {', '.join(unmatched)}"
            )
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
                          instruments: dict = None,
                          reduce_only: bool = False) -> dict:
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

        # BUG CORRIGIDO: lia os.environ diretamente com default "10", ignorando
        # o valor de config.py. Se LEVERAGE não estivesse setado no Railway,
        # a ordem ia com 10x mesmo com o config em 50x.
        from bot.config import cfg as _cfg
        _lev = int(_cfg.LEVERAGE)

        body: dict = {
            "clientOid": _oid,
            "symbol":    kc_sym,
            "side":      kc_side,
            "type":      "market",
            "size":      str(contracts),
            "leverage":  str(_lev),
        }

        # reduceOnly (auditoria #3): garante que a ordem SÓ pode reduzir uma
        # posição existente. Sem essa flag, se a qty do bot divergir da qty
        # real na exchange, a ordem de "fechamento" abriria posição na direção
        # oposta, dobrando a exposição em vez de zerá-la.
        if reduce_only:
            body["reduceOnly"] = True
            body["closeOrder"] = True   # KuCoin: fecha a posição do símbolo

        # SL server-side
        if sl > 0:
            body["stop"]          = "down" if side == "Buy" else "up"
            body["stopPrice"]     = self._round_price(sl, symbol)
            # CORRIGIDO (auditoria #6): "TP" = Trade/Last Price, não mark price.
            # Usar last price deixa o SL vulnerável a stop hunt em pares de
            # baixa liquidez. "MP" = Mark Price (preço justo, resistente a
            # manipulação por poucos negócios).
            body["stopPriceType"] = os.environ.get("KUCOIN_STOP_PRICE_TYPE", "MP")

        # TP server-side
        if tp > 0:
            # KuCoin Futures v2: takeProfit separado
            body["takeProfit"] = self._round_price(tp, symbol)

        data     = await self._post("/api/v1/orders", body)
        order_id = data.get("orderId", "")

        # Fallback de leverage: nem todo par KuCoin permite 50x.
        # Altcoins costumam ter limite de 20x-25x. Se a ordem falhar,
        # retenta com valores menores até conseguir.
        if not order_id and _lev > 20:
            for fallback_lev in (25, 20, 10):
                if fallback_lev >= _lev:
                    continue
                log.warning(
                    f"⚠️ Ordem {symbol} falhou com {_lev}x — "
                    f"retentando com {fallback_lev}x"
                )
                body["leverage"] = str(fallback_lev)
                body["clientOid"] = hashlib.md5(
                    f"{_raw}_{fallback_lev}".encode()
                ).hexdigest()[:40]
                data     = await self._post("/api/v1/orders", body)
                order_id = data.get("orderId", "")
                if order_id:
                    log.info(f"✓ Ordem aceita com {fallback_lev}x")
                    break

        if order_id:
            log.info(
                f"📤 Ordem {side} {contracts} contratos {symbol} "
                f"@ {body['leverage']}x → orderId={order_id}"
            )
        else:
            log.error(f"❌ Ordem {symbol} rejeitada em todas as tentativas")
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
        kc_sym   = to_kucoin(symbol)
        gran     = INTERVAL_MAP.get(str(interval), int(interval))   # granularidade em minutos

        # Timestamps em MILISSEGUNDOS (exigência da KuCoin Futures).
        # int() explícito evita notação científica em números grandes.
        end_ts   = int(time.time() * 1000)
        span_ms  = int(gran) * 60 * 1000 * int(limit)   # minutos → ms × qtd candles

        # KuCoin limita o range por requisição. Cap de 200 dias evita
        # "from" muito antigo, que a API rejeita.
        MAX_SPAN_MS = 200 * 24 * 60 * 60 * 1000
        span_ms  = min(span_ms, MAX_SPAN_MS)
        start_ts = max(0, end_ts - span_ms)

        # KuCoin Futures kline endpoint correto
        # granularity em minutos: 1, 5, 15, 30, 60, 120, 240, 480, 720, 1440, 10080
        # CORRIGIDO: KuCoin Futures exige 'from'/'to' em MILISSEGUNDOS.
        # Erro anterior: "300000 Parameter 'from' must be milliseconds"
        data = await self._get("/api/v1/kline/query", {
            "symbol":      kc_sym,
            "granularity": str(gran),
            "from":        str(start_ts),   # milissegundos
            "to":          str(end_ts),     # milissegundos
        })

        klines = []
        raw    = data if isinstance(data, list) else []
        for k in raw:
            # KuCoin Futures kline: [timestamp_ms, open, high, low, close, volume, turnover]
            # Nota: KuCoin retorna do mais recente para o mais antigo — inverter no final
            try:
                _ts = int(float(k[0]))
                # KuCoin Futures retorna ts em ms; normaliza caso venha em segundos
                if _ts < 1e11:
                    _ts *= 1000
                klines.append({
                    "ts": _ts,
                    "o":  float(k[1]),
                    "h":  float(k[2]),
                    "l":  float(k[3]),
                    "c":  float(k[4]),
                    "v":  float(k[5]),
                })
            except (IndexError, ValueError, TypeError) as _e:
                # auditoria #10: descarte de candle malformado agora é contado.
                # Log em nível debug para não poluir, mas rastreável.
                self._malformed_klines = getattr(self, "_malformed_klines", 0) + 1
                if self._malformed_klines % 50 == 1:
                    log.debug(
                        f"kline malformada descartada ({self._malformed_klines} "
                        f"no total): {_e}"
                    )
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
        """
        Retorna posições abertas formatadas igual ao BybitClient.
        CORRIGIDO: trata resposta vazia e erro 400004 sem quebrar o engine.
        """
        try:
            data = await self._get("/api/v1/positions", auth=True)
        except Exception as e:
            log.warning(f"get_positions erro: {e}")
            return []

        if not data:
            return []

        positions = []
        # KuCoin pode retornar lista direta ou dict com lista
        raw = data if isinstance(data, list) else data.get("data", [])
        if not isinstance(raw, list):
            return []

        for p in raw:
            try:
                qty = float(p.get("currentQty", 0))
                if qty == 0:
                    continue
                kc_sym = p.get("symbol", "")
                _entry = float(p.get("avgEntryPrice", 0))
                positions.append({
                    "symbol":           to_standard(kc_sym),
                    "side":             "Buy" if qty > 0 else "Sell",
                    "size":             abs(qty),
                    "entryPrice":       _entry,
                    # Alias para compatibilidade com código que esperava o
                    # formato Bybit (auditoria #2)
                    "avgPrice":         _entry,
                    "markPrice":        float(p.get("markPrice", 0)),
                    "unrealisedPnl":    float(p.get("unrealisedPnl", 0)),
                    "leverage":         float(p.get("realLeverage", 1)),
                    # ADICIONADO (auditoria #2): liquidationPrice não era
                    # exposto, fazendo o engine calcular SL com liq=0.
                    "liquidationPrice": float(p.get("liquidationPrice", 0)),
                    "liqPrice":         float(p.get("liquidationPrice", 0)),
                    "posMargin":        float(p.get("posMargin", 0)),
                })
            except (ValueError, TypeError) as _e:
                # auditoria #10: posição com campos inválidos agora é logada
                log.warning(
                    f"posição descartada por campo inválido: {_e} | "
                    f"dados: {str(p)[:150]}"
                )
                continue
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
            # BUG CORRIGIDO: antes logava sucesso mesmo com token vazio,
            # mascarando falha de autenticação no WS.
            if self._ws_token:
                log.info(f"✓ WS token KuCoin obtido ({len(self._ws_token)} chars)")
            else:
                log.error("❌ WS token VAZIO — autenticação KuCoin falhou (verifique credenciais)")
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
    async def sync_time(self) -> bool:
        """
        Sincroniza o relógio local com o servidor KuCoin.
        KuCoin rejeita requisições com timestamp fora de ±5s da hora do servidor,
        retornando 400005 Invalid KC-API-SIGN (mensagem enganosa).
        Railway/containers frequentemente têm drift de relógio.
        """
        try:
            await self._ensure_session()
            local_before = int(time.time() * 1000)
            async with self._session.get(REST_BASE + "/api/v1/timestamp") as r:
                data = await r.json()
            local_after = int(time.time() * 1000)
            server_ts   = int(data.get("data", 0))
            if server_ts <= 0:
                log.warning("sync_time: servidor não retornou timestamp")
                return False
            # Compensa latência da requisição (metade do round-trip)
            local_mid           = (local_before + local_after) // 2
            self._time_offset_ms = server_ts - local_mid
            self._time_synced    = True
            log.info(
                f"🕐 Tempo sincronizado com KuCoin | offset={self._time_offset_ms}ms"
                + ("  ⚠️ DRIFT ALTO!" if abs(self._time_offset_ms) > 3000 else "")
            )
            return True
        except Exception as e:
            log.warning(f"sync_time falhou: {e}")
            return False

    def _now_ms(self) -> int:
        """Timestamp em ms ajustado pelo offset do servidor KuCoin."""
        return int(time.time() * 1000) + self._time_offset_ms

    async def ping(self) -> bool:
        """
        Verifica conectividade com a KuCoin API.
        Usa endpoint público de tempo do servidor (sem autenticação).
        Adicionado para compatibilidade com engine.py que chama client.ping().
        """
        try:
            data = await self._get("/api/v1/timestamp")
            return bool(data)
        except Exception as e:
            log.warning(f"KuCoin ping falhou: {e}")
            return False

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        log.info("👋 KuCoinClient encerrado")
