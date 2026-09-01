"""
NEXUS-7 — Testes de regressão: connected=True + viable_symbols=[]

Reproduz os cenários da auditoria que encontrou o bug: falha de API no
boot deixava o bot "conectado" para sempre com zero pares, sem retry.

DESENHO DE ISOLAMENTO: bot.kucoin lê KUCOIN_REST_BASE (e outras env
vars) uma ÚNICA VEZ no import do módulo — nível de módulo, não por
instância. Rodar múltiplos testes com portas de mock diferentes no
mesmo processo Python quebra silenciosamente (o cliente sempre usa a
PRIMEIRA porta importada). Por isso todos os testes deste arquivo
compartilham UMA porta fixa e UM único servidor mock, isolados entre
si via /_reset + /_faults (endpoints já existentes no mock) em vez de
reimportar ou trocar REST_BASE.

Rodar: python -m tests.test_viable_symbols_recovery
"""
import asyncio, json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_P = _F = 0
def check(n, c, x=""):
    global _P, _F
    if c: _P += 1; print(f"  ✓ {n}")
    else: _F += 1; print(f"  ✗ {n} {x}")

_PORT = 8899

os.environ.update({
    "KUCOIN_REST_BASE": f"http://127.0.0.1:{_PORT}",
    "KUCOIN_API_KEY": "k", "KUCOIN_API_SECRET": "s",
    "KUCOIN_API_PASSPHRASE": "p", "PAPER_TRADE": "false",
    "LIVE_TRADING_CONFIRMED": "I_UNDERSTAND_THE_RISK",
    "BOT_API_SECRET": "t", "LOG_LEVEL": "ERROR",
    "LEVERAGE": "50", "MAX_RISK_PCT": "2.0", "MAX_MARGIN_PCT": "0.98",
    "NEXUS_AI_ENABLED": "false", "NEXUS_TELEGRAM": "false",
})


async def _reset_mock():
    # BUG DE INFRAESTRUTURA DE TESTE CORRIGIDO: urllib.request é
    # SÍNCRONO/bloqueante. Chamado de dentro de uma corrotina asyncio
    # que compartilha o mesmo event loop do servidor mock, ele trava a
    # conexão TCP local indefinidamente (o loop nunca cede controle
    # para o servidor processar a requisição). Trocado por aiohttp,
    # assíncrono de verdade, compatível com o loop já em execução.
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.post(f"http://127.0.0.1:{_PORT}/_reset",
                          json={}, timeout=aiohttp.ClientTimeout(total=5)):
            pass


async def _set_fault(**kw):
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.post(f"http://127.0.0.1:{_PORT}/_faults",
                          json=kw, timeout=aiohttp.ClientTimeout(total=5)):
            pass


async def _novo_engine():
    """Cria um par (client, engine) fresco para um teste, sem reconectar
    o mock (que já está rodando e é compartilhado por todos os testes)."""
    from bot.kucoin import KuCoinClient
    from bot.engine import TradingEngine
    c = KuCoinClient()
    e = TradingEngine(c)
    return c, e


async def test_A_api_falha_inicialmente():
    """TESTE A: /contracts/active falha → viable_symbols==[] → 0 ordens."""
    await _reset_mock()
    await _set_fault(contracts_fail_next=99)   # falha em toda tentativa deste teste

    c, e = await _novo_engine()
    await c.load_instruments()
    await e._connect()

    check("viable_symbols == [] com API falhando", e.viable_symbols == [])

    import tests.mock_kucoin as MK
    MK.ORDERS.clear()
    await e._scan_all_and_enter()
    check("nenhuma ordem HTTP enviada", len(MK.ORDERS) == 0)

    await c.close()
    await _set_fault(contracts_fail_next=0)   # limpa para o próximo teste


async def test_B_api_recupera_apos_falhas():
    """TESTE B: falha, falha, sucesso → viable_symbols > 0, scan volta."""
    await _reset_mock()
    await _set_fault(contracts_fail_next=2)   # as 2 primeiras chamadas falham

    c, e = await _novo_engine()
    await c.load_instruments()   # chamada #1 -> falha (consome 1 do fault)
    await e._connect()           # chamada #2 -> falha (consome o outro)

    check("viable_symbols == [] logo após boot com falha dupla",
          e.viable_symbols == [])

    recuperou = False
    for _ in range(5):
        e._viable_retry_next_ts = 0.0   # zera o backoff só para o teste
        recuperou = await e._ensure_viable_symbols()
        if recuperou:
            break

    check("retry recupera após a API voltar", recuperou is True)
    check("viable_symbols > 0 após recovery",
          len(e.viable_symbols) > 0, f"={len(e.viable_symbols)}")
    check("retry_attempt reseta para 0 após sucesso",
          e._viable_retry_attempt == 0)

    await c.close()


async def test_C_saldo_zero():
    """TESTE C: saldo=0 → viable_symbols==[] → 0 ordens."""
    await _reset_mock()
    await _set_fault(balance_override=0.0)

    c, e = await _novo_engine()
    await c.load_instruments()
    await e._connect()

    check("instrumentos carregam mesmo com saldo zero",
          len(e.instruments) > 0)
    check("viable_symbols == [] com saldo zero", e.viable_symbols == [])

    import tests.mock_kucoin as MK
    MK.ORDERS.clear()
    await e._scan_all_and_enter()
    check("nenhuma ordem enviada com saldo zero", len(MK.ORDERS) == 0)

    await c.close()
    await _set_fault(balance_override=None)


async def test_D_sem_preco():
    """
    TESTE D: instrumento existe mas preço indisponível → viable==[].

    Simulado via contracts_fail_next aplicado SÓ na 2ª chamada (a de
    get_all_tickers dentro de _filter_viable_symbols), preservando a
    1ª chamada (load_instruments) intacta — isso reproduz exatamente
    "instrumento existe, preço não".
    """
    await _reset_mock()

    c, e = await _novo_engine()
    await c.load_instruments()      # instrumentos carregam OK
    check("instrumentos carregam", len(c.get_instruments()) > 0)

    await _set_fault(contracts_fail_next=1)   # próxima chamada (tickers) falha
    e.instruments = c.get_instruments()
    ok = await e._filter_viable_symbols()

    check("_filter_viable_symbols retorna False sem preços", ok is False)
    check("viable_symbols == [] sem preço disponível", e.viable_symbols == [])

    import tests.mock_kucoin as MK
    MK.ORDERS.clear()
    await e._scan_all_and_enter()
    check("nenhuma ordem enviada sem preço", len(MK.ORDERS) == 0)

    await c.close()


async def test_E_nao_chama_open():
    """TESTE E: viable_symbols=[] → _scan_all_and_enter não chega em _open."""
    await _reset_mock()
    c, e = await _novo_engine()
    e.viable_symbols = []   # força o estado, sem depender de rede

    _chamou_open = {"v": False}
    async def _fake_open(sig):
        _chamou_open["v"] = True
    e._open = _fake_open

    await e._scan_all_and_enter()
    check("_open() NUNCA foi chamado com viable_symbols=[]",
          _chamou_open["v"] is False)

    await c.close()


async def test_F_recovery_nao_duplica_ordem():
    """
    TESTE F: viable_symbols vazio -> API recupera -> scan roda -> a
    idempotência via clientOid continua intacta (nenhuma duplicação
    introduzida pelo mecanismo de retry).
    """
    await _reset_mock()
    await _set_fault(contracts_fail_next=1)

    c, e = await _novo_engine()
    await c.load_instruments()   # falha
    await e._connect()

    for _ in range(3):
        e._viable_retry_next_ts = 0.0
        if await e._ensure_viable_symbols():
            break

    check("recovery concluído antes do teste de idempotência",
          len(e.viable_symbols) > 0)

    import tests.mock_kucoin as MK
    inst = c.get_instruments()
    MK.ORDERS.clear()
    # 3 chamadas concorrentes do MESMO sinal, como já validado em
    # sessões anteriores — aqui confirma que o retry não alterou esse
    # comportamento.
    await asyncio.gather(*[
        c.place_order("BTCUSDT", "Buy", 0.002, sl=100000, tp=120000,
                      instruments=inst, idem_key="pos_recovery_test")
        for _ in range(3)], return_exceptions=True)
    oids = {o.get("clientOid") for o in MK.ORDERS if o.get("clientOid")}
    check("idempotência preservada após recovery (1 clientOid)",
          len(oids) == 1, f"oids={oids}")

    await c.close()


async def test_G_backoff_nao_agressivo():
    """TESTE G: falhas consecutivas respeitam backoff crescente."""
    await _reset_mock()
    await _set_fault(contracts_fail_next=99)

    c, e = await _novo_engine()
    await c.load_instruments()
    await e._connect()

    tempos = []
    for _ in range(4):
        e._viable_retry_next_ts = 0.0   # força tentar imediatamente
        antes = e._viable_retry_attempt
        await e._ensure_viable_symbols()
        depois = e._viable_retry_attempt
        tempos.append(e._viable_retry_next_ts - time.time())
        check(f"tentativa incrementa contador ({antes}->{depois})",
              depois == antes + 1)

    check("backoff cresce entre tentativas (não é flat)",
          tempos[-1] > tempos[0], f"tempos={[round(t,1) for t in tempos]}")
    check("backoff respeita teto máximo (<=300s)",
          all(t <= 300 for t in tempos))

    await c.close()
    await _set_fault(contracts_fail_next=0)


async def _run_all():
    global _P, _F
    from aiohttp import web
    import tests.mock_kucoin as MK

    runner = web.AppRunner(MK.make_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", _PORT)
    await site.start()

    testes = [
        test_A_api_falha_inicialmente,
        test_B_api_recupera_apos_falhas,
        test_C_saldo_zero,
        test_D_sem_preco,
        test_E_nao_chama_open,
        test_F_recovery_nao_duplica_ordem,
        test_G_backoff_nao_agressivo,
    ]
    for t in testes:
        print(f"\n{t.__name__}:")
        try:
            await t()
        except Exception as ex:
            _F += 1
            import traceback
            print(f"  ✗ ERRO: {type(ex).__name__}: {ex}")
            traceback.print_exc()

    await runner.cleanup()


if __name__ == "__main__":
    print("═══ TESTES: connected=True + viable_symbols=[] ═══")
    asyncio.run(_run_all())
    print(f"\n{'='*50}\nPASSOU: {_P} | FALHOU: {_F}\n{'='*50}")
    sys.exit(1 if _F else 0)
