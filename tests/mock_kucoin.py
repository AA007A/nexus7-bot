"""
Mock da API KuCoin Futures — permite testar o caminho COMPLETO do bot
(startup → scan → sinal → ordem) sem tocar na exchange real.

Retorna dados realistas: 12 contratos ativos, candles com tendência
forte (para gerar score alto), saldo, funding, OI.
"""
import json, math, random, time
from aiohttp import web


def _gen_klines(n: int, interval_min: int, base: float = 100.0,
                trend: float = 0.004, seed: int = 42):
    """
    Candles realistas: tendência de alta COM pullbacks.

    Uma alta monotônica leva o RSI a 100 e dispara o bloqueio de "RSI
    extremo" — comportamento correto do bot, mas irreal. Mercado de
    verdade tem correções, e é isso que este gerador simula.
    """
    # Determinismo: sem isto, cada chamada gera uma série diferente e o
    # teste vira loteria — 4H e 1H podem discordar por puro artefato.
    # Timestamps ANCORADOS NO PRESENTE (o preço é determinístico pela
    # seed, mas o tempo precisa ser atual — senão os candles parecem
    # antigos e o NEXUS penaliza a qualidade dos dados corretamente).
    random.seed(seed)
    now_ms = int(time.time() * 1000)
    step   = interval_min * 60 * 1000
    out, px = [], base
    for i in range(n):
        # Tendência de alta com pullbacks RASOS e pouco frequentes:
        # mantém 4H e 1H alinhados (bull) e o RSI fora do extremo.
        # Pullbacks longos dessincronizavam os timeframes e o bot
        # corretamente recusava operar.
        # Alta com respirações curtas: mantém 4H e 1H em bull mas segura
        # o RSI abaixo de 92 (alta monotônica leva o RSI a 100 e o bot
        # corretamente bloqueia por "RSI extremo").
        respiro = -0.55 if (i % 7 == 0) else 1.0
        px *= (1 + trend * respiro + random.gauss(0, 0.0012))
        o = px * (1 + random.gauss(0, 0.0005))
        h = max(o, px) * (1 + abs(random.gauss(0, 0.001)))
        l = min(o, px) * (1 - abs(random.gauss(0, 0.001)))
        v = 1000 * (1 + i / n) * (1 + abs(random.gauss(0, 0.2)))
        ts = now_ms - (n - i) * step
        # KuCoin REST: [ts, open, high, low, close, volume]
        out.append([ts, round(o, 4), round(h, 4), round(l, 4),
                    round(px, 4), round(v, 2)])
    return out


SYMBOLS = ["XBTUSDTM", "ETHUSDTM", "SOLUSDTM", "XRPUSDTM", "ADAUSDTM",
           "DOGEUSDTM", "LINKUSDTM", "AVAXUSDTM", "DOTUSDTM", "LTCUSDTM",
           "NEARUSDTM", "ATOMUSDTM"]

PRICES = {"XBTUSDTM": 108000, "ETHUSDTM": 3900, "SOLUSDTM": 140,
          "XRPUSDTM": 2.1, "ADAUSDTM": 0.9, "DOGEUSDTM": 0.19,
          "LINKUSDTM": 22, "AVAXUSDTM": 35, "DOTUSDTM": 7,
          "LTCUSDTM": 95, "NEARUSDTM": 5, "ATOMUSDTM": 6}

MULT = {"XBTUSDTM": 0.001, "ETHUSDTM": 0.01, "SOLUSDTM": 0.1,
        "XRPUSDTM": 10, "ADAUSDTM": 10, "DOGEUSDTM": 100,
        "LINKUSDTM": 0.1, "AVAXUSDTM": 0.1, "DOTUSDTM": 1,
        "LTCUSDTM": 0.01, "NEARUSDTM": 1, "ATOMUSDTM": 1}

ORDERS = []          # registro de ordens recebidas
STOPS  = {}          # SL/TP aplicados por símbolo
POSITIONS = {}       # posições abertas



# ── FAULT INJECTOR (Fase 4G) ──────────────────────────────────────
# Permite ao teste forçar falhas específicas sem alterar o bot.
FAULTS = {
    "rate_limit_next": 0,    # próximas N respostas retornam 429
    "ws_drop_after":   0,    # derruba o WS após N mensagens
    "ws_duplicate":    False,# reenvia cada evento duas vezes
    "ws_out_of_order": False,# inverte a ordem dos candles
    "ws_no_pong":      False,# ignora pings (heartbeat failure)
    "order_no_reply":  False,# executa a ordem mas não responde
}


async def set_faults(req):
    body = await req.json()
    FAULTS.update(body)
    return web.json_response({"ok": True, "faults": FAULTS})


def _maybe_429():
    """Consome uma falha de rate limit, se agendada."""
    if FAULTS["rate_limit_next"] > 0:
        FAULTS["rate_limit_next"] -= 1
        return web.json_response(
            {"code": "429000", "msg": "Too Many Requests"}, status=429)
    return None


async def contracts_active(req):
    _f = _maybe_429()
    if _f is not None: return _f
    data = []
    for s in SYMBOLS:
        data.append({
            "symbol": s, "status": "Open",
            "baseCurrency": s.replace("USDTM", ""),
            "lotSize": 1, "tickSize": 0.0001,
            "multiplier": MULT[s], "maxLeverage": 100,
            "lastTradePrice": PRICES[s],
            "turnoverOf24h": 500_000_000, "volumeOf24h": 1_000_000,
            "priceChgPct": 0.02,
        })
    return web.json_response({"code": "200000", "data": data})


async def account_overview(req):
    _f = _maybe_429()
    if _f is not None: return _f
    return web.json_response({"code": "200000", "data": {
        "accountEquity": 100.0, "availableBalance": 100.0,
        "currency": "USDT",
    }})


async def kline_query(req):
    _f = _maybe_429()
    if _f is not None: return _f
    sym = req.query.get("symbol", "XBTUSDTM")
    gran = int(req.query.get("granularity", 15))
    n = 200 if gran == 15 else (150 if gran == 60 else 120)
    base = PRICES.get(sym, 100)
    seed = abs(hash(sym)) % 1000
    return web.json_response({
        "code": "200000",
        "data": _gen_klines(n, gran, base, 0.004, seed),
    })


async def positions(req):
    _f = _maybe_429()
    if _f is not None: return _f
    return web.json_response({"code": "200000", "data": list(POSITIONS.values())})


async def orders(req):
    _f = _maybe_429()
    if _f is not None: return _f
    body = await req.json()
    oid  = f"mock_{len(ORDERS)}_{int(time.time()*1000)}"
    ORDERS.append({**body, "orderId": oid, "ts": time.time()})
    sym  = body.get("symbol", "")
    size = int(body.get("size", 0))
    side = body.get("side", "buy")
    if not body.get("reduceOnly"):
        POSITIONS[sym] = {
            "symbol": sym,
            "currentQty": size if side == "buy" else -size,
            "avgEntryPrice": PRICES.get(sym, 100),
            "markPrice": PRICES.get(sym, 100),
            "unrealisedPnl": 0.0, "realLeverage": int(body.get("leverage", 1)),
            "liquidationPrice": PRICES.get(sym, 100) * 0.98,
            "posMargin": 10.0,
            "stopLoss": STOPS.get(sym, {}).get("sl", 0),
            "takeProfit": STOPS.get(sym, {}).get("tp", 0),
        }
    else:
        POSITIONS.pop(sym, None)
    if FAULTS["order_no_reply"]:
        # Cenário C: exchange EXECUTA mas a resposta não chega ao bot
        raise web.HTTPGatewayTimeout(text="simulated: no reply")
    return web.json_response({"code": "200000", "data": {"orderId": oid}})


async def trading_stop(req):
    body = await req.json()
    sym  = body.get("symbol", "")
    STOPS[sym] = {"sl": float(body.get("stopLoss", 0) or 0),
                  "tp": float(body.get("takeProfit", 0) or 0)}
    if sym in POSITIONS:
        POSITIONS[sym]["stopLoss"]   = STOPS[sym]["sl"]
        POSITIONS[sym]["takeProfit"] = STOPS[sym]["tp"]
    return web.json_response({"code": "200000", "data": True})


async def bullet_private(req):
    return web.json_response({"code": "200000", "data": {
        "token": "mock_token_" + "x" * 200,
        "instanceServers": [{
            "endpoint": "ws://127.0.0.1:8788/ws",
            "pingInterval": 18000, "pingTimeout": 10000,
        }],
    }})


async def timestamp(req):
    return web.json_response({"code": "200000", "data": int(time.time() * 1000)})


async def funding_rate(req):
    return web.json_response({"code": "200000", "data": {"value": 0.0001}})


async def stats(req):
    """Endpoint de introspecção para os testes lerem o estado do servidor."""
    return web.json_response({
        "orders": ORDERS, "stops": STOPS,
        "positions": list(POSITIONS.values()),
        "n_orders": len(ORDERS),
        "client_oids": sorted({o.get("clientOid") for o in ORDERS if o.get("clientOid")}),
    })


async def reset(req):
    ORDERS.clear(); STOPS.clear(); POSITIONS.clear()
    return web.json_response({"ok": True})


async def catch_all(req):
    return web.json_response({"code": "200000", "data": {}})


async def ws_handler(req):
    ws = web.WebSocketResponse()
    await ws.prepare(req)
    await ws.send_json({"type": "welcome", "id": "1"})
    async for msg in ws:
        if msg.type == web.WSMsgType.TEXT:
            try:
                d = json.loads(msg.data)
                if d.get("type") == "subscribe":
                    await ws.send_json({"type": "ack", "id": d.get("id")})
                elif d.get("type") == "ping":
                    await ws.send_json({"type": "pong", "id": d.get("id")})
            except Exception:
                pass
    return ws


def make_app():
    app = web.Application()
    app.router.add_get("/api/v1/contracts/active", contracts_active)
    app.router.add_get("/api/v1/account-overview", account_overview)
    app.router.add_get("/api/v1/kline/query", kline_query)
    app.router.add_get("/api/v1/positions", positions)
    app.router.add_get("/api/v1/timestamp", timestamp)
    app.router.add_get("/api/v1/funding-rate/{sym}/current", funding_rate)
    app.router.add_post("/api/v1/orders", orders)
    app.router.add_post("/api/v1/position/trading-stop", trading_stop)
    app.router.add_post("/api/v1/bullet-private", bullet_private)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/_stats", stats)
    app.router.add_post("/_reset", reset)
    app.router.add_post("/_faults", set_faults)
    app.router.add_route("*", "/{tail:.*}", catch_all)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="127.0.0.1", port=8788, print=None)
