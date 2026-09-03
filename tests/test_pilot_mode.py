"""
NEXUS-7 — Testes: modo piloto controlado (REAL_TRADING_PILOT)

Camada de gate ADICIONAL sobre as barreiras existentes. Quando
REAL_TRADING_PILOT não está definido, o módulo é INERTE — nenhum
comportamento anterior muda.

Rodar: python -m tests.test_pilot_mode
"""
import asyncio, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_P = _F = 0
def check(n, c, x=""):
    global _P, _F
    if c: _P += 1; print(f"  ✓ {n}")
    else: _F += 1; print(f"  ✗ {n} {x}")

_PORT = 9110
os.environ.update({
    "KUCOIN_REST_BASE": f"http://127.0.0.1:{_PORT}",
    "KUCOIN_API_KEY": "k", "KUCOIN_API_SECRET": "s",
    "KUCOIN_API_PASSPHRASE": "p", "PAPER_TRADE": "false",
    "LIVE_TRADING_CONFIRMED": "I_UNDERSTAND_THE_RISK",
    "BOT_API_SECRET": "t", "LOG_LEVEL": "ERROR",
    "LEVERAGE": "10", "MAX_RISK_PCT": "0.5", "MAX_MARGIN_PCT": "0.98",
    "NEXUS_AI_ENABLED": "false", "NEXUS_TELEGRAM": "false",
})


class _AiOk:
    execution_allowed = True
    setup_quality = 80.0
    confidence = 75.0
    reasoning = ["aprovado"]


class _AiVeto:
    execution_allowed = False
    setup_quality = 40.0
    confidence = 30.0
    reasoning = ["EV negativo"]


async def _engine():
    from bot.kucoin import KuCoinClient
    from bot.engine import TradingEngine
    c = KuCoinClient(); e = TradingEngine(c)
    await c.load_instruments(); await e._connect()
    return c, e


def test_PILOT01_inerte_quando_desabilitado():
    """PILOT-01: sem REAL_TRADING_PILOT, o gate é inerte (não bloqueia)."""
    import bot.pilot as P
    orig = P.PILOT_ENABLED
    P.PILOT_ENABLED = False
    g = P.PilotGuard()
    check("PILOT-01: enabled=False quando a env var não está definida",
          not g.enabled)
    check("PILOT-01: can_open_pilot devolve True (inerte)",
          g.can_open_pilot(None, None, "BTCUSDT", None) is True)
    P.PILOT_ENABLED = orig


async def test_PILOT02_bloqueia_saldo_zero():
    """PILOT-02 (req. 3): saldo Futures USDT = 0 bloqueia."""
    import bot.pilot as P
    P.PILOT_ENABLED = True
    c, e = await _engine()
    e.risk.balance = 0.0
    motivos = e.pilot.evaluate(e, c, "DOGEUSDT", _AiOk())
    check("PILOT-02: saldo zero é bloqueio",
          any("3_BALANCE" in m for m in motivos), str(motivos))
    check("PILOT-02: can_open_pilot=False",
          not e.pilot.can_open_pilot(e, c, "DOGEUSDT", _AiOk()))
    await c.close()


async def test_PILOT03_bloqueia_sem_confirmacao_de_conta():
    """PILOT-03 (req. 2): conta real não confirmada bloqueia."""
    import bot.pilot as P
    P.PILOT_ENABLED = True
    os.environ.pop("PILOT_ACCOUNT_CONFIRMED", None)
    c, e = await _engine()
    motivos = e.pilot.evaluate(e, c, "DOGEUSDT", _AiOk())
    check("PILOT-03: exige PILOT_ACCOUNT_CONFIRMED explícito",
          any("2_ACCOUNT" in m for m in motivos), str(motivos))
    await c.close()


async def test_PILOT04_bloqueia_sem_ai():
    """PILOT-04 (req. 10): sem decisão do NEXUS AI, bloqueia."""
    import bot.pilot as P
    P.PILOT_ENABLED = True
    c, e = await _engine()
    motivos = e.pilot.evaluate(e, c, "DOGEUSDT", None)
    check("PILOT-04: ausência de decisão AI é bloqueio",
          any("10_AI" in m for m in motivos), str(motivos))
    motivos2 = e.pilot.evaluate(e, c, "DOGEUSDT", _AiVeto())
    check("PILOT-04: veto do AI é bloqueio",
          any("10_AI" in m for m in motivos2), str(motivos2))
    await c.close()


async def test_PILOT05_bloqueia_unprotected():
    """PILOT-05 (req. 7/8): símbolo desprotegido bloqueia."""
    import bot.pilot as P
    P.PILOT_ENABLED = True
    c, e = await _engine()
    e._unprotected_symbols.add("DOGEUSDT")
    motivos = e.pilot.evaluate(e, c, "DOGEUSDT", _AiOk())
    check("PILOT-05: _unprotected_symbols bloqueia",
          any("7_8_UNPROTECTED" in m for m in motivos), str(motivos))
    e._unprotected_symbols.clear()
    await c.close()


async def test_PILOT06_limite_uma_posicao_simultanea():
    """PILOT-06: MAX_CONCURRENT_POSITIONS = 1 no piloto."""
    import bot.pilot as P
    from bot.engine import Position, Signal
    P.PILOT_ENABLED = True
    c, e = await _engine()
    sig = Signal("BTCUSDT", "LONG", 100.0, 98.0, 106.0, 80, "x", 3.0)
    e.positions["BTCUSDT"] = Position(sig, qty=1.0)
    motivos = e.pilot.evaluate(e, c, "DOGEUSDT", _AiOk())
    check("PILOT-06: 1 posição aberta já bloqueia nova entrada",
          any("PILOT_CONCURRENT" in m for m in motivos), str(motivos))
    e.positions.clear()
    await c.close()


async def test_PILOT07_limite_uma_ordem_por_sessao():
    """PILOT-07: MAX_NEW_POSITIONS_PER_SESSION = 1."""
    import bot.pilot as P
    P.PILOT_ENABLED = True
    c, e = await _engine()
    e.pilot.register_position_opened("BTCUSDT")
    e.positions.clear()   # mesmo sem posição aberta, a sessão já contou
    motivos = e.pilot.evaluate(e, c, "DOGEUSDT", _AiOk())
    check("PILOT-07: 1 ordem/sessão bloqueia a segunda",
          any("PILOT_SESSION" in m for m in motivos), str(motivos))
    await c.close()


async def test_PILOT08_bloqueia_market_data_stale():
    """PILOT-08 (req. 11): market data velha bloqueia."""
    import bot.pilot as P
    P.PILOT_ENABLED = True
    c, e = await _engine()
    c._last_ws_update = time.time() - 9999
    motivos = e.pilot.evaluate(e, c, "DOGEUSDT", _AiOk())
    check("PILOT-08: market data stale bloqueia",
          any("11_MARKET_DATA" in m for m in motivos), str(motivos))
    await c.close()


async def test_PILOT09_fail_closed_em_erro():
    """PILOT-09: erro na própria avaliação conta como bloqueio."""
    import bot.pilot as P
    P.PILOT_ENABLED = True
    g = P.PilotGuard()
    motivos = g.evaluate(None, None, "DOGEUSDT", _AiOk())
    check("PILOT-09: engine None não libera (fail-closed)",
          len(motivos) > 0, str(motivos))
    check("PILOT-09: can_open_pilot=False com engine inválido",
          not g.can_open_pilot(None, None, "DOGEUSDT", _AiOk()))


async def test_PILOT10_nao_afrouxa_barreiras_existentes():
    """
    PILOT-10: o piloto NUNCA libera algo que outra barreira bloqueou —
    ele só adiciona motivos para não operar. Verificado por leitura:
    o gate é chamado DEPOIS do NEXUS AI e não altera nenhuma condição
    anterior.
    """
    import inspect, re
    from bot import engine as E
    src = inspect.getsource(E)
    # Busca DENTRO de _open() — o arquivo tem outros place_order em
    # caminhos de fechamento (reduce_only), que não são relevantes aqui.
    _m = re.search(r"    async def _open\(self.*?(?=\n    async def )", src, re.S)
    corpo = _m.group(0)
    i_ai = corpo.find("nx_dec is not None and not nx_dec.execution_allowed")
    i_pilot = corpo.find("self.pilot.can_open_pilot")
    i_place = corpo.find("await self.client.place_order")
    check("PILOT-10: gate do piloto vem DEPOIS do veto do NEXUS AI",
          -1 < i_ai < i_pilot, f"ai={i_ai} pilot={i_pilot}")
    check("PILOT-10: gate do piloto vem ANTES de place_order",
          -1 < i_pilot < i_place, f"pilot={i_pilot} place={i_place}")
    check("PILOT-10: [AI_DECISION] é logado dentro de _open, antes da ordem",
          "[AI_DECISION]" in corpo and corpo.find("[AI_DECISION]") < i_place)
    check("PILOT-10: piloto bloqueia se NEXUS AI não produziu decisão",
          "Piloto não permite fallback silencioso" in corpo)


async def test_PILOT11_barreira_paper_live_intacta():
    """PILOT-11: a barreira PAPER_TRADE/LIVE_TRADING_CONFIRMED não mudou."""
    import inspect
    from bot import kucoin as K
    src = inspect.getsource(K)
    check("PILOT-11: exige PAPER_TRADE=false E LIVE_TRADING_CONFIRMED",
          '_paper_env == "false" and _live_ack == _LIVE_TOKEN' in src)
    check("PILOT-11: config ausente/ambígua → PAPER (fail-safe)",
          'PAPER — PAPER_TRADE ausente ou inválido' in src)
    check("PILOT-11: piloto não altera bot/kucoin.py",
          "REAL_TRADING_PILOT" not in src)


async def _run_all():
    from aiohttp import web
    import tests.mock_kucoin as MK
    runner = web.AppRunner(MK.make_app()); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", _PORT); await site.start()

    print("\ntest_PILOT01_inerte_quando_desabilitado:")
    test_PILOT01_inerte_quando_desabilitado()

    for fn in [test_PILOT02_bloqueia_saldo_zero,
               test_PILOT03_bloqueia_sem_confirmacao_de_conta,
               test_PILOT04_bloqueia_sem_ai,
               test_PILOT05_bloqueia_unprotected,
               test_PILOT06_limite_uma_posicao_simultanea,
               test_PILOT07_limite_uma_ordem_por_sessao,
               test_PILOT08_bloqueia_market_data_stale,
               test_PILOT09_fail_closed_em_erro,
               test_PILOT10_nao_afrouxa_barreiras_existentes,
               test_PILOT11_barreira_paper_live_intacta]:
        print(f"\n{fn.__name__}:")
        try:
            await fn()
        except Exception as ex:
            global _F
            _F += 1
            import traceback
            print(f"  ✗ ERRO: {type(ex).__name__}: {ex}")
            traceback.print_exc()

    await runner.cleanup()


if __name__ == "__main__":
    print("═══ TESTES DO MODO PILOTO CONTROLADO ═══")
    asyncio.run(_run_all())
    print(f"\n{'='*50}\nPASSOU: {_P} | FALHOU: {_F}\n{'='*50}")
    sys.exit(1 if _F else 0)
