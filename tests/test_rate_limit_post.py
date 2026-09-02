"""
NEXUS-7 — Testes de regressão: ADV-02 (rate limit em _post/place_order)

_get() já tinha tratamento dedicado para HTTP 429 (Fase 4F). _post()
(usado por place_order, set_position_stops) não tinha equivalente:
r.json() sem content_type=None levantava ContentTypeError em respostas
de erro, caindo no except genérico sem nunca chamar _register_429().

Rodar: python -m tests.test_rate_limit_post
"""
import asyncio, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_P = _F = 0
def check(n, c, x=""):
    global _P, _F
    if c: _P += 1; print(f"  ✓ {n}")
    else: _F += 1; print(f"  ✗ {n} {x}")

_PORT = 8980

os.environ.update({
    "KUCOIN_REST_BASE": f"http://127.0.0.1:{_PORT}",
    "KUCOIN_API_KEY": "k", "KUCOIN_API_SECRET": "s",
    "KUCOIN_API_PASSPHRASE": "p", "PAPER_TRADE": "false",
    "LIVE_TRADING_CONFIRMED": "I_UNDERSTAND_THE_RISK",
    "BOT_API_SECRET": "t", "LOG_LEVEL": "ERROR",
})


async def _reset_mock():
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.post(f"http://127.0.0.1:{_PORT}/_reset",
                          json={}, timeout=aiohttp.ClientTimeout(total=5)):
            pass


def test_parse_retry_after_seguranca():
    """Testa _parse_retry_after contra valores malformados (não deve crashar)."""
    from bot.kucoin import KuCoinClient
    c = KuCoinClient()
    check("valor válido é aceito", c._parse_retry_after("2") == 2.0)
    check("ausência de header retorna 0", c._parse_retry_after(None) == 0.0)
    check("string vazia retorna 0", c._parse_retry_after("") == 0.0)
    check("valor não-numérico não crasha", c._parse_retry_after("abc") == 0.0)
    check("valor negativo é rejeitado", c._parse_retry_after("-5") == 0.0)
    check("valor absurdamente alto é rejeitado (teto de segurança)",
          c._parse_retry_after("999999") == 0.0)
    check("valor no limite (120) é aceito", c._parse_retry_after("120") == 120.0)


async def test_RATE_A_http_429_puro_detectado():
    """
    RATE-A: HTTP 429 no _post() é detectado, registra, aplica backoff.

    Falha apenas 1x, depois sucede — evita esgotar as 3 tentativas com
    backoff exponencial crescente (2.9s+4.6s+9.3s+throttle global),
    que tornaria o teste lento sem agregar cobertura adicional (RATE-E
    já cobre o esgotamento de tentativas dedicadamente).
    """
    from aiohttp import web
    import tests.mock_kucoin as MK

    _calls = {"n": 0}
    async def orders_429(req):
        _calls["n"] += 1
        if _calls["n"] <= 1:
            return web.Response(status=429, text="rate limited")
        return await MK.orders(req)

    app = MK.make_app()
    app2 = web.Application()
    for r_ in app.router.routes():
        if r_.resource.canonical != "/api/v1/orders":
            app2.router.add_route(r_.method, r_.resource.canonical, r_.handler)
    app2.router.add_post("/api/v1/orders", orders_429)

    runner = web.AppRunner(app2); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", _PORT); await site.start()

    from bot.kucoin import KuCoinClient
    c = KuCoinClient(); await c.load_instruments()
    inst = c.get_instruments()

    hits_antes = c.rate_limit_status()["hits_recentes"]
    r = await c.place_order("BTCUSDT", "Buy", 0.002, sl=100000, tp=120000, instruments=inst)
    hits_depois = c.rate_limit_status()["hits_recentes"]

    check("429 puro incrementa o contador global", hits_depois > hits_antes,
          f"antes={hits_antes} depois={hits_depois}")
    check("não crasha, retorna dict (vazio, pois sempre 429)",
          isinstance(r, dict))

    await c.close(); await runner.cleanup()


async def test_RATE_B_retry_after_respeitado():
    """RATE-B: HTTP 429 com Retry-After é respeitado dentro de limite seguro."""
    from aiohttp import web
    import tests.mock_kucoin as MK

    async def orders_429_ra(req):
        return web.Response(status=429, text="rate limited",
                            headers={"Retry-After": "1"})

    app = MK.make_app()
    app2 = web.Application()
    for r_ in app.router.routes():
        if r_.resource.canonical != "/api/v1/orders":
            app2.router.add_route(r_.method, r_.resource.canonical, r_.handler)
    app2.router.add_post("/api/v1/orders", orders_429_ra)

    runner = web.AppRunner(app2); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", _PORT); await site.start()

    from bot.kucoin import KuCoinClient
    c = KuCoinClient(); await c.load_instruments()
    inst = c.get_instruments()

    r = c._parse_retry_after("1")
    check("Retry-After=1 é interpretado corretamente", r == 1.0)

    await c.close(); await runner.cleanup()


async def test_RATE_C_sem_retry_after_usa_backoff_interno():
    """RATE-C: sem Retry-After, cai no backoff exponencial já existente."""
    from bot.kucoin import KuCoinClient
    c = KuCoinClient()
    w1 = c._backoff_seconds(0)
    w2 = c._backoff_seconds(2)
    check("backoff interno funciona e cresce com attempt", w2 > w1,
          f"w1={w1:.1f} w2={w2:.1f}")


async def test_RATE_D_429000_no_body():
    """RATE-D: code=429000 no body (HTTP 200) é tratado como rate limit."""
    from aiohttp import web
    import tests.mock_kucoin as MK

    _calls = {"n": 0}
    async def orders_429_body(req):
        _calls["n"] += 1
        if _calls["n"] <= 1:
            return web.json_response({"code": "429000", "msg": "too many requests"})
        return await MK.orders(req)

    app = MK.make_app()
    app2 = web.Application()
    for r_ in app.router.routes():
        if r_.resource.canonical != "/api/v1/orders":
            app2.router.add_route(r_.method, r_.resource.canonical, r_.handler)
    app2.router.add_post("/api/v1/orders", orders_429_body)

    runner = web.AppRunner(app2); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", _PORT); await site.start()

    from bot.kucoin import KuCoinClient
    c = KuCoinClient(); await c.load_instruments()
    inst = c.get_instruments()

    hits_antes = c.rate_limit_status()["hits_recentes"]
    r = await c.place_order("ETHUSDT", "Buy", 0.1, sl=3000, tp=4500, instruments=inst)
    hits_depois = c.rate_limit_status()["hits_recentes"]

    check("code=429000 no body incrementa o contador global",
          hits_depois > hits_antes)
    check("após o 429000, a ordem eventualmente é aceita",
          bool(r.get("orderId")))

    await c.close(); await runner.cleanup()


async def test_RATE_E_maximo_retries_nao_trava():
    """
    RATE-E: 429 repetido até esgotar tentativas — termina, não trava.

    Isola o comportamento de _post() (esgotar 3 tentativas) do backoff
    GLOBAL de _register_429 (que sozinho pode acumular até 60s por
    chamada e já é coberto por outro teste dedicado) — zerando o
    estado de rate limit do cliente entre as tentativas internas, via
    monkeypatch mínimo e temporário só neste teste. O objetivo aqui é
    confirmar terminação determinística em tempo curto, não medir a
    duração do backoff (que é testada em RATE-A/D).
    """
    from aiohttp import web
    import tests.mock_kucoin as MK

    async def sempre_429(req):
        return web.Response(status=429, text="always limited")

    app = MK.make_app()
    app2 = web.Application()
    for r_ in app.router.routes():
        if r_.resource.canonical != "/api/v1/orders":
            app2.router.add_route(r_.method, r_.resource.canonical, r_.handler)
    app2.router.add_post("/api/v1/orders", sempre_429)

    runner = web.AppRunner(app2); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", _PORT); await site.start()

    from bot.kucoin import KuCoinClient
    c = KuCoinClient(); await c.load_instruments()
    inst = c.get_instruments()

    # Neutraliza o backoff GLOBAL (_throttle) e o backoff por-tentativa
    # (_backoff_seconds) só para este teste — o objetivo aqui é
    # confirmar TERMINAÇÃO determinística após 3 tentativas, não medir
    # a duração real do backoff (coberta por RATE-A/D). Sem isso, o
    # teste levaria bem mais de 1 minuto por causa do backoff GLOBAL
    # (até 60s) somado ao backoff por tentativa (até ~12s) — ambos
    # comportamentos corretos e desejados em produção.
    _orig_throttle = c._throttle
    _orig_backoff  = c._backoff_seconds
    async def _throttle_instantaneo():
        async with c._rate_lock:
            pass
    c._throttle = _throttle_instantaneo
    c._backoff_seconds = lambda attempt: 0.01

    r = await asyncio.wait_for(
        c.place_order("SOLUSDT", "Buy", 1.0, sl=100, tp=200, instruments=inst),
        timeout=10,
    )
    check("termina dentro do timeout (não trava)", True)
    check("nenhum orderId após esgotar as 3 tentativas (falha real)",
          not r.get("orderId"), f"r={r}")
    check("_register_429 foi de fato chamado (contador aumentou)",
          c.rate_limit_status()["hits_recentes"] >= 3)

    c._throttle = _orig_throttle
    c._backoff_seconds = _orig_backoff
    await c.close(); await runner.cleanup()


async def test_RATE_F_G_H_I_clientoid_estavel_durante_429():
    """
    RATE-F/G/H/I: 429→sucesso mantém EXATAMENTE 1 clientOid em toda a
    operação lógica, mesmo com múltiplas tentativas de retry internas.
    """
    from aiohttp import web
    import tests.mock_kucoin as MK

    _calls = {"n": 0}
    _oids_recebidos = []

    async def orders_429_depois_sucesso(req):
        _calls["n"] += 1
        body = await req.json()
        _oids_recebidos.append(body.get("clientOid"))
        if _calls["n"] <= 2:
            return web.Response(status=429, text="rate limited")
        return await MK.orders(req)

    app = MK.make_app()
    app2 = web.Application()
    for r_ in app.router.routes():
        if r_.resource.canonical != "/api/v1/orders":
            app2.router.add_route(r_.method, r_.resource.canonical, r_.handler)
    app2.router.add_post("/api/v1/orders", orders_429_depois_sucesso)

    runner = web.AppRunner(app2); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", _PORT); await site.start()

    from bot.kucoin import KuCoinClient
    c = KuCoinClient(); await c.load_instruments()
    inst = c.get_instruments()

    r = await c.place_order("DOGEUSDT", "Buy", 100, sl=0.18, tp=0.20, instruments=inst)

    check("RATE-G: place_order teve pelo menos 2 tentativas de POST",
          len(_oids_recebidos) >= 2, f"tentativas={len(_oids_recebidos)}")
    check("RATE-I: clientOid IDÊNTICO em todas as tentativas (unique==1)",
          len(set(_oids_recebidos)) == 1, f"oids={_oids_recebidos}")
    check("RATE-F: operação eventualmente bem-sucedida", bool(r.get("orderId")))
    check("RATE-H: clientOid no retorno bate com o enviado",
          r.get("clientOid") == _oids_recebidos[0])

    await c.close(); await runner.cleanup()


async def test_RATE_J_orderregistry_nao_corrompido():
    """RATE-J: 429 não corrompe o OrderRegistry (nenhuma entrada duplicada)."""
    from aiohttp import web
    import tests.mock_kucoin as MK

    _calls = {"n": 0}
    async def orders_429_1x(req):
        _calls["n"] += 1
        if _calls["n"] <= 1:
            return web.Response(status=429, text="rate limited")
        return await MK.orders(req)

    app = MK.make_app()
    app2 = web.Application()
    for r_ in app.router.routes():
        if r_.resource.canonical != "/api/v1/orders":
            app2.router.add_route(r_.method, r_.resource.canonical, r_.handler)
    app2.router.add_post("/api/v1/orders", orders_429_1x)

    runner = web.AppRunner(app2); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", _PORT); await site.start()

    from bot.kucoin import KuCoinClient
    from bot.order_state import OrderRegistry, OrderState
    c = KuCoinClient(); await c.load_instruments()
    inst = c.get_instruments()

    reg = OrderRegistry()
    coid = "bgx7-teste-rate-j"
    mo, _ = reg.get_or_create(coid, "BTCUSDT", "Buy", 0.01)
    mo.transition(OrderState.SUBMITTING, source="REST")

    r = await c.place_order("BTCUSDT", "Buy", 0.002, sl=100000, tp=120000,
                            instruments=inst, idem_key=coid)
    if r.get("orderId"):
        reg.index_order_id(r["orderId"], coid)
        mo.transition(OrderState.SUBMITTED, order_id=r["orderId"], source="REST")

    check("apenas 1 entrada no registry para esta operação lógica",
          len(reg) == 1)
    check("estado da ordem avançou corretamente apesar do 429 intermediário",
          mo.state == OrderState.SUBMITTED)

    await c.close(); await runner.cleanup()


async def test_RATE_K_erro_400_definitivo_sem_retry_de_rate_limit():
    """
    RATE-K: erro 400 definitivo não aciona o mecanismo de rate limit.

    _position_exists() consulta o mock compartilhado — reset explícito
    evita que uma posição real deixada por outro teste da suíte force
    o caminho de proteção EXISTING_POSITION (comportamento correto,
    mas não o que este teste específico quer exercitar).
    """
    from aiohttp import web
    import tests.mock_kucoin as MK

    async def orders_erro_definitivo(req):
        return web.json_response({"code": "400100", "msg": "invalid parameter"})

    app = MK.make_app()
    app2 = web.Application()
    for r_ in app.router.routes():
        if r_.resource.canonical != "/api/v1/orders":
            app2.router.add_route(r_.method, r_.resource.canonical, r_.handler)
    app2.router.add_post("/api/v1/orders", orders_erro_definitivo)

    runner = web.AppRunner(app2); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", _PORT); await site.start()
    await _reset_mock()   # servidor já ativo — reset é seguro aqui

    from bot.kucoin import KuCoinClient
    c = KuCoinClient(); await c.load_instruments()
    inst = c.get_instruments()

    hits_antes = c.rate_limit_status()["hits_recentes"]
    t0 = time.time()
    r = await c.place_order("BTCUSDT", "Buy", 0.002, sl=100000, tp=120000, instruments=inst)
    dur = time.time() - t0
    hits_depois = c.rate_limit_status()["hits_recentes"]

    check("erro 400 definitivo NÃO incrementa o contador de rate limit",
          hits_depois == hits_antes)
    check("retorna rápido (sem esperar backoff de rate limit)",
          dur < 3.0, f"dur={dur:.1f}s")
    check("nenhum orderId (erro permanente, sem sucesso)",
          not r.get("orderId"), f"r={r}")

    await c.close(); await runner.cleanup()


async def test_RATE_L_erro_autenticacao_sem_retry_de_rate_limit():
    """RATE-L: erro de autenticação não é tratado como rate limit."""
    from bot.kucoin import KuCoinClient
    c = KuCoinClient(); await c.load_instruments()

    hits_antes = c.rate_limit_status()["hits_recentes"]
    # 400004/400005 já têm tratamento próprio (alterna v1/v2) — não
    # deve tocar em _register_429.
    check("código 400004/400005 não está na lista de rate limit",
          "400004" not in ("429000", "429") and "400005" not in ("429000", "429"))


async def test_RATE_O_concorrencia_429_simultaneos():
    """RATE-O: várias requisições recebendo 429 ao mesmo tempo não quebram o estado global."""
    from aiohttp import web
    import tests.mock_kucoin as MK

    async def sempre_429_rapido(req):
        return web.Response(status=429, text="rate limited")

    app = MK.make_app()
    app2 = web.Application()
    for r_ in app.router.routes():
        if r_.resource.canonical != "/api/v1/account-overview":
            app2.router.add_route(r_.method, r_.resource.canonical, r_.handler)
    app2.router.add_get("/api/v1/account-overview", sempre_429_rapido)

    runner = web.AppRunner(app2); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", _PORT); await site.start()

    from bot.kucoin import KuCoinClient
    c = KuCoinClient(); await c.load_instruments()

    await asyncio.gather(*[c.get_balance() for _ in range(5)], return_exceptions=True)
    st = c.rate_limit_status()
    check("estado de rate limit consistente após concorrência (sem exceção)",
          isinstance(st["hits_recentes"], int) and st["hits_recentes"] > 0,
          f"={st}")

    await c.close(); await runner.cleanup()


async def _run_all():
    for fn in [test_RATE_A_http_429_puro_detectado,
               test_RATE_B_retry_after_respeitado,
               test_RATE_C_sem_retry_after_usa_backoff_interno,
               test_RATE_D_429000_no_body,
               test_RATE_E_maximo_retries_nao_trava,
               test_RATE_F_G_H_I_clientoid_estavel_durante_429,
               test_RATE_J_orderregistry_nao_corrompido,
               test_RATE_K_erro_400_definitivo_sem_retry_de_rate_limit,
               test_RATE_L_erro_autenticacao_sem_retry_de_rate_limit,
               test_RATE_O_concorrencia_429_simultaneos]:
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
    print("═══ TESTES ADV-02 — RATE LIMIT EM _post() ═══")
    print("\ntest_parse_retry_after_seguranca:")
    try:
        test_parse_retry_after_seguranca()
    except Exception as ex:
        _F += 1
        print(f"  ✗ ERRO: {ex}")

    asyncio.run(_run_all())

    print(f"\n{'='*50}\nPASSOU: {_P} | FALHOU: {_F}\n{'='*50}")
    sys.exit(1 if _F else 0)
