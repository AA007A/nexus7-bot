"""
NEXUS-7 — Testes: EXEC-02 (ordem ambígua / orderId ausente)

EXEC-02 (CRITICAL): get_order_status("") agrupava `not order_id` com
PAPER_TRADE e orderIds sintéticos legítimos, retornando _synthetic=True
— que wait_for_fill() interpreta como filled=True.

Cadeia do bug:
  place_order falha 3x → data = {}
  → data["clientOid"] = _oid torna o dict NÃO-vazio
  → _open() faz _order.get("orderId","") = ""
  → get_order_status("") → _synthetic → filled=True
  → Position criada e "✅ ABERTO" logado para ordem NUNCA aceita.

Rodar: python -m tests.test_exec02_ambiguous_order
"""
import asyncio, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_P = _F = 0
def check(n, c, x=""):
    global _P, _F
    if c: _P += 1; print(f"  ✓ {n}")
    else: _F += 1; print(f"  ✗ {n} {x}")

_PORT = 9020

os.environ.update({
    "KUCOIN_REST_BASE": f"http://127.0.0.1:{_PORT}",
    "KUCOIN_API_KEY": "k", "KUCOIN_API_SECRET": "s",
    "KUCOIN_API_PASSPHRASE": "p", "PAPER_TRADE": "false",
    "LIVE_TRADING_CONFIRMED": "I_UNDERSTAND_THE_RISK",
    "BOT_API_SECRET": "t", "LOG_LEVEL": "ERROR",
    "LEVERAGE": "10", "MAX_RISK_PCT": "0.5", "MAX_MARGIN_PCT": "0.98",
    "NEXUS_AI_ENABLED": "false", "NEXUS_TELEGRAM": "false",
})


async def test_EXEC02_orderid_vazio_nao_e_fill():
    """EXEC-02: orderId vazio NUNCA pode retornar filled=True."""
    from bot.kucoin import KuCoinClient
    c = KuCoinClient()

    st = await c.get_order_status("")
    check("orderId vazio NÃO retorna _synthetic",
          not st.get("_synthetic"), f"={st}")
    check("orderId vazio marca estado como desconhecido",
          st.get("_unknown") is True, f"={st}")
    check("orderId vazio mantém isActive=True (não confirmado)",
          st.get("isActive") is True)

    r = await c.wait_for_fill("", timeout_s=1.0, poll_interval_s=0.3)
    check("wait_for_fill('') NÃO reporta filled=True",
          r["filled"] is False, f"={r}")

    await c.close()


async def test_EXEC02_orderids_sinteticos_legitimos_ainda_funcionam():
    """Não-regressão: paper_* e EXISTING_POSITION continuam sintéticos."""
    from bot.kucoin import KuCoinClient
    c = KuCoinClient()

    st1 = await c.get_order_status("paper_12345")
    check("paper_* continua sintético", st1.get("_synthetic") is True)

    st2 = await c.get_order_status("EXISTING_POSITION")
    check("EXISTING_POSITION continua sintético", st2.get("_synthetic") is True)

    r = await c.wait_for_fill("paper_12345")
    check("wait_for_fill de paper_* ainda resolve filled=True",
          r["filled"] is True)

    await c.close()


async def test_B2_ordem_aceita_resposta_perdida_converge():
    """
    B2 (o cenário mais crítico): a EXCHANGE aceita e registra a ordem
    E a posição, e SÓ ENTÃO a resposta se perde. Diferente de timeout
    antes da exchange receber.

    Exigido: o bot não pode declarar ABERTO falsamente, e deve
    convergir posteriormente (descobrir e proteger a posição real).
    """
    from aiohttp import web
    import tests.mock_kucoin as MK

    async def aceita_mas_perde_resposta(req):
        body = await req.json()
        oid = f"mock_{len(MK.ORDERS)}_perdida"
        MK.ORDERS.append({**body, "orderId": oid})
        sym = body.get("symbol", "")
        size = int(body.get("size", 0))
        if not body.get("reduceOnly"):
            MK.POSITIONS[sym] = {
                "symbol": sym,
                "currentQty": size if body.get("side") == "buy" else -size,
                "avgEntryPrice": MK.PRICES.get(sym, 100),
                "markPrice": MK.PRICES.get(sym, 100),
                "unrealisedPnl": 0.0, "realLeverage": 10,
                "liquidationPrice": MK.PRICES.get(sym, 100) * 0.98,
                "posMargin": 10.0, "stopLoss": 0, "takeProfit": 0,
            }
        raise web.HTTPGatewayTimeout(text="response lost AFTER exchange accepted")

    app = MK.make_app()
    app2 = web.Application()
    for r_ in app.router.routes():
        if r_.resource.canonical != "/api/v1/orders":
            app2.router.add_route(r_.method, r_.resource.canonical, r_.handler)
    app2.router.add_post("/api/v1/orders", aceita_mas_perde_resposta)

    runner = web.AppRunner(app2); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", _PORT); await site.start()

    from bot.kucoin import KuCoinClient
    from bot.engine import TradingEngine, Signal
    c = KuCoinClient(); e = TradingEngine(c)
    await c.load_instruments(); await e._connect()

    sig = Signal(symbol="DOGEUSDT", direction="LONG", confidence=80,
                 entry=0.19, sl=0.188, tp=0.20, score=80,
                 expected_pnl=1.0, reason="B2")
    MK.ORDERS.clear(); MK.POSITIONS.clear()

    try:
        await asyncio.wait_for(e._open(sig), timeout=120)
    except Exception:
        pass

    check("exchange REALMENTE tem a posição (cenário montado corretamente)",
          len(MK.POSITIONS) == 1, f"={len(MK.POSITIONS)}")
    check("bot NÃO declara posição aberta sem orderId confirmado",
          "DOGEUSDT" not in e.positions,
          "declarou ABERTO sem confirmação — EXEC-02 regrediu")

    # CONVERGÊNCIA: o guardião periódico deve descobrir e proteger
    async with e._pos_lock:
        await e._guard_naked_positions()

    check("B2: posição órfã é DESCOBERTA pelo guardião periódico",
          "DOGEUSDT" in e.positions,
          "não convergiu — exposição real ficaria invisível")
    if "DOGEUSDT" in e.positions:
        p = e.positions["DOGEUSDT"]
        check("B2: qty em unidade base correta (EXEC-01 mantido)",
              abs(p.qty - 2600.0) < 1.0, f"qty={p.qty}")
        check("B2: posição recebe STOP LOSS após descoberta",
              p.sl > 0, f"sl={p.sl}")
        check("B2: não fica marcada como desprotegida",
              "DOGEUSDT" not in e._unprotected_symbols)

    await c.close(); await runner.cleanup()


async def _run_all():
    for fn in [test_EXEC02_orderid_vazio_nao_e_fill,
               test_EXEC02_orderids_sinteticos_legitimos_ainda_funcionam,
               test_B2_ordem_aceita_resposta_perdida_converge]:
        print(f"\n{fn.__name__}:")
        try:
            await fn()
        except Exception as ex:
            global _F
            _F += 1
            import traceback
            print(f"  ✗ ERRO: {type(ex).__name__}: {ex}")
            traceback.print_exc()


if __name__ == "__main__":
    print("═══ TESTES EXEC-02 / B2 — ORDEM AMBÍGUA ═══")
    asyncio.run(_run_all())
    print(f"\n{'='*50}\nPASSOU: {_P} | FALHOU: {_F}\n{'='*50}")
    sys.exit(1 if _F else 0)
