"""
NEXUS-7 — Testes de regressão: ADV-margin

RiskManager._margin_in_use() sempre retornava $0.00 porque
open_position_risk()/close_position_risk() nunca eram chamados por
bot/engine.py. Corrigido fazendo size()/_margin_in_use() aceitarem
engine.positions (a fonte real de posições confirmadas, já mantida
correta por TP parcial e pela reconciliação do ADV-01) como parâmetro
opcional, em vez de depender exclusivamente do self.positions interno
do RiskManager (que permanece desconectado, por decisão de não
duplicar/reescrever a arquitetura existente).

Rodar: python -m tests.test_margin_reconciliation
"""
import asyncio, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_P = _F = 0
def check(n, c, x=""):
    global _P, _F
    if c: _P += 1; print(f"  ✓ {n}")
    else: _F += 1; print(f"  ✗ {n} {x}")

_PORT = 8961

os.environ.update({
    "KUCOIN_REST_BASE": f"http://127.0.0.1:{_PORT}",
    "KUCOIN_API_KEY": "k", "KUCOIN_API_SECRET": "s",
    "KUCOIN_API_PASSPHRASE": "p", "PAPER_TRADE": "false",
    "LIVE_TRADING_CONFIRMED": "I_UNDERSTAND_THE_RISK",
    "BOT_API_SECRET": "t", "LOG_LEVEL": "ERROR",
    "LEVERAGE": "10", "MAX_RISK_PCT": "0.5", "MAX_MARGIN_PCT": "0.98",
    "NEXUS_AI_ENABLED": "false", "NEXUS_TELEGRAM": "false",
})


async def _reset_mock():
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.post(f"http://127.0.0.1:{_PORT}/_reset",
                          json={}, timeout=aiohttp.ClientTimeout(total=5)):
            pass


def test_MARGIN_K_sem_posicoes_e_zero():
    """MARGIN-K: sem posições, _margin_in_use() == 0."""
    from bot.risk import RiskManager
    r = RiskManager()
    r.init(1000.0)
    check("sem posições, margin_in_use == 0",
          r._margin_in_use({}) == 0.0)
    check("sem open_positions (None), usa self.positions vazio == 0",
          r._margin_in_use() == 0.0)


def test_MARGIN_A_posicao_confirmada_aparece():
    """MARGIN-A: uma posição confirmada aparece no cálculo de margem."""
    from bot.risk import RiskManager
    from bot.engine import Position, Signal

    r = RiskManager(); r.init(1000.0)
    sig = Signal("BTCUSDT", "LONG", 100.0, 98.0, 106.0, 80, "teste", 3.0)
    pos = Position(sig, qty=10.0)

    m = r._margin_in_use({"BTCUSDT": pos})
    esperado = (100.0 * 10.0) / 10  # entry*qty/leverage, LEVERAGE=10
    check("margem reflete posição confirmada",
          abs(m - esperado) < 0.01, f"={m} esperado={esperado}")
    check("MARGIN-L: posição existente não resulta em margin_in_use==0",
          m > 0)


def test_MARGIN_B_duas_posicoes_somam():
    """MARGIN-B: duas posições somam corretamente a margem."""
    from bot.risk import RiskManager
    from bot.engine import Position, Signal

    r = RiskManager(); r.init(1000.0)
    sig_a = Signal("BTCUSDT", "LONG", 100.0, 98.0, 106.0, 80, "a", 3.0)
    sig_b = Signal("ETHUSDT", "LONG", 50.0, 49.0, 53.0, 80, "b", 3.0)
    pos_a = Position(sig_a, qty=10.0)
    pos_b = Position(sig_b, qty=20.0)

    m = r._margin_in_use({"BTCUSDT": pos_a, "ETHUSDT": pos_b})
    esperado = (100.0*10.0)/10 + (50.0*20.0)/10
    check("margem = soma das duas posições",
          abs(m - esperado) < 0.01, f"={m} esperado={esperado}")


def test_MARGIN_C_posicao_fechada_nao_consome_margem():
    """MARGIN-C: posição fechada (removida de positions) não consome margem."""
    from bot.risk import RiskManager
    from bot.engine import Position, Signal

    r = RiskManager(); r.init(1000.0)
    sig = Signal("BTCUSDT", "LONG", 100.0, 98.0, 106.0, 80, "teste", 3.0)
    pos = Position(sig, qty=10.0)
    positions = {"BTCUSDT": pos}

    m1 = r._margin_in_use(positions)
    check("margem > 0 com posição aberta", m1 > 0)

    del positions["BTCUSDT"]   # simula fechamento (engine faz isso)
    m2 = r._margin_in_use(positions)
    check("margem == 0 após remoção (fechamento)", m2 == 0.0)


def test_MARGIN_D_E_partial_fill_reflete_qty_efetiva():
    """MARGIN-D/E: partial fill 50% e 90% contabilizam só a exposição efetiva."""
    from bot.risk import RiskManager
    from bot.engine import Position, Signal

    r = RiskManager(); r.init(1000.0)
    sig = Signal("BTCUSDT", "LONG", 100.0, 98.0, 106.0, 80, "teste", 3.0)

    for pct, label in [(0.5, "50%"), (0.9, "90%")]:
        qty_efetiva = 100.0 * pct   # ordem de 100, preenchida a `pct`
        pos = Position(sig, qty=qty_efetiva)
        m = r._margin_in_use({"BTCUSDT": pos})
        esperado = (100.0 * qty_efetiva) / 10
        check(f"partial fill {label}: margem reflete qty efetiva ({qty_efetiva})",
              abs(m - esperado) < 0.01, f"={m} esperado={esperado}")
        # E não o tamanho ORIGINAL solicitado (100)
        margem_100 = (100.0 * 100.0) / 10
        check(f"partial fill {label}: margem NÃO é a do tamanho solicitado (100)",
              abs(m - margem_100) > 0.01)


async def test_MARGIN_F_reconciliacao_registra_margem():
    """MARGIN-F: posição órfã reconciliada (ADV-01) é contabilizada."""
    await _reset_mock()
    import tests.mock_kucoin as MK
    from bot.kucoin import KuCoinClient
    from bot.engine import TradingEngine

    c = KuCoinClient(); e = TradingEngine(c)
    await c.load_instruments(); await e._connect()
    inst = c.get_instruments()

    await c.place_order("ETHUSDT", "Buy", 0.1, sl=0, tp=0, instruments=inst)
    e.positions.clear()

    m_antes = e.risk._margin_in_use(e.positions)
    check("antes da reconciliação, margem == 0 (posição órfã ainda não vista)",
          m_antes == 0.0)

    await e._reconcile_exchange_positions()
    m_depois = e.risk._margin_in_use(e.positions)
    check("após reconciliação, margem reflete a posição órfã",
          m_depois > 0, f"={m_depois}")

    await c.close()


async def test_MARGIN_G_reconciliacao_repetida_nao_duplica():
    """MARGIN-G: reconciliação chamada 2x não duplica margem."""
    await _reset_mock()
    import tests.mock_kucoin as MK
    from bot.kucoin import KuCoinClient
    from bot.engine import TradingEngine

    c = KuCoinClient(); e = TradingEngine(c)
    await c.load_instruments(); await e._connect()
    inst = c.get_instruments()

    await c.place_order("SOLUSDT", "Buy", 1.0, sl=0, tp=0, instruments=inst)
    e.positions.clear()

    await e._reconcile_exchange_positions()
    m1 = e.risk._margin_in_use(e.positions)
    await e._reconcile_exchange_positions()   # repete
    m2 = e.risk._margin_in_use(e.positions)

    check("margem idêntica após reconciliação repetida (sem duplicar)",
          abs(m1 - m2) < 0.01, f"m1={m1} m2={m2}")

    await c.close()


async def test_MARGIN_H_open_e_reconciliation_concorrentes():
    """MARGIN-H: abertura normal + reconciliação concorrente não duplicam exposição."""
    await _reset_mock()
    import tests.mock_kucoin as MK
    from bot.kucoin import KuCoinClient
    from bot.engine import TradingEngine

    c = KuCoinClient(); e = TradingEngine(c)
    await c.load_instruments(); await e._connect()
    inst = c.get_instruments()

    await c.place_order("AVAXUSDT", "Buy", 5.0, sl=0, tp=0, instruments=inst)
    e.positions.clear()

    await asyncio.gather(
        e._reconcile_exchange_positions(),
        e._reconcile_exchange_positions(),
    )
    check("apenas 1 entrada em positions após reconciliações concorrentes",
          len(e.positions) == 1, f"={list(e.positions.keys())}")
    m = e.risk._margin_in_use(e.positions)
    check("margem não duplicada", m > 0)

    await c.close()


async def test_MARGIN_I_restart_reconstroi_corretamente():
    """MARGIN-I: _load_existing_positions (restart) reconstrói margem corretamente."""
    await _reset_mock()
    import tests.mock_kucoin as MK
    from bot.kucoin import KuCoinClient
    from bot.engine import TradingEngine

    c = KuCoinClient(); e = TradingEngine(c)
    await c.load_instruments(); await e._connect()
    inst = c.get_instruments()

    await c.place_order("LTCUSDT", "Buy", 2.0, sl=0, tp=0, instruments=inst)

    # Simula restart: novo engine, mesmo client
    e2 = TradingEngine(c)
    await e2._load_existing_positions()

    m = e2.risk._margin_in_use(e2.positions)
    check("após restart, margem reflete posição pré-existente",
          m > 0, f"={m}")
    check("exatamente 1 posição restaurada", len(e2.positions) == 1)

    await c.close()


def test_MARGIN_J_fechamento_parcial_reduz_margem():
    """MARGIN-J: fechamento parcial (100→60) reduz a margem proporcionalmente."""
    from bot.risk import RiskManager
    from bot.engine import Position, Signal

    r = RiskManager(); r.init(1000.0)
    sig = Signal("BTCUSDT", "LONG", 100.0, 98.0, 106.0, 80, "teste", 3.0)
    pos = Position(sig, qty=100.0)
    positions = {"BTCUSDT": pos}

    m_100 = r._margin_in_use(positions)
    pos.qty = 60.0   # simula _manage_partial_tp reduzindo qty
    m_60 = r._margin_in_use(positions)

    check("margem com qty=100 é maior que com qty=60", m_100 > m_60)
    check("proporção respeitada (60/100 do valor original)",
          abs(m_60 - m_100 * 0.6) < 0.01, f"m100={m_100} m60={m_60}")


def test_duplicidade_open_position_risk_legado():
    """
    DUPLICIDADE (requisito 8): open_position_risk() chamado duas vezes
    para o mesmo símbolo não duplica exposição no caminho LEGADO
    (self.positions do RiskManager, usado quando open_positions=None).
    """
    from bot.risk import RiskManager
    from bot.strategy import Signal as StratSignal

    r = RiskManager(); r.init(1000.0)

    class SigFake:
        symbol = "BTCUSDT"; direction = "LONG"
        entry = 100.0; sl = 98.0; tp = 106.0
        tp1 = 103.0; tp2 = 106.0

    r.open_position_risk(SigFake(), qty=10.0)
    r.open_position_risk(SigFake(), qty=10.0)   # chamado 2x
    check("apenas 1 entrada em self.positions (sobrescreve, não duplica)",
          len(r.positions) == 1)
    m = r._margin_in_use()   # caminho legado, sem open_positions
    esperado = (100.0 * 10.0) / 10
    check("margem legada não duplicada", abs(m - esperado) < 0.01)


def test_invariante_engine_positions_vs_risk_margin():
    """
    TESTE DE INVARIANTE: para qualquer conjunto de engine.positions,
    _margin_in_use(engine.positions) deve ser exatamente a soma de
    entry*qty/LEVERAGE de cada posição — nunca 0 se houver posições
    com entry>0 e qty>0.
    """
    from bot.risk import RiskManager
    from bot.engine import Position, Signal

    r = RiskManager(); r.init(1000.0)
    posicoes = {}
    esperado_total = 0.0
    for i, (sym, entry, qty) in enumerate([
        ("BTCUSDT", 100.0, 5.0), ("ETHUSDT", 50.0, 8.0), ("SOLUSDT", 20.0, 15.0),
    ]):
        sig = Signal(sym, "LONG", entry, entry*0.98, entry*1.06, 80, "inv", 3.0)
        posicoes[sym] = Position(sig, qty=qty)
        esperado_total += (entry * qty) / 10

    m = r._margin_in_use(posicoes)
    check("invariante: margem == soma exata das posições confirmadas",
          abs(m - esperado_total) < 0.01, f"={m} esperado={esperado_total}")
    check("invariante: com posições reais, margem NUNCA é 0",
          m > 0)


async def _run_async_tests():
    from aiohttp import web
    import tests.mock_kucoin as MK
    runner = web.AppRunner(MK.make_app()); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", _PORT); await site.start()

    for fn in [test_MARGIN_F_reconciliacao_registra_margem,
               test_MARGIN_G_reconciliacao_repetida_nao_duplica,
               test_MARGIN_H_open_e_reconciliation_concorrentes,
               test_MARGIN_I_restart_reconstroi_corretamente]:
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
    print("═══ TESTES ADV-margin ═══")
    for fn in [test_MARGIN_K_sem_posicoes_e_zero,
               test_MARGIN_A_posicao_confirmada_aparece,
               test_MARGIN_B_duas_posicoes_somam,
               test_MARGIN_C_posicao_fechada_nao_consome_margem,
               test_MARGIN_D_E_partial_fill_reflete_qty_efetiva,
               test_MARGIN_J_fechamento_parcial_reduz_margem,
               test_duplicidade_open_position_risk_legado,
               test_invariante_engine_positions_vs_risk_margin]:
        print(f"\n{fn.__name__}:")
        try:
            fn()
        except Exception as ex:
            _F += 1
            import traceback
            print(f"  ✗ ERRO: {type(ex).__name__}: {ex}")
            traceback.print_exc()

    asyncio.run(_run_async_tests())

    print(f"\n{'='*50}\nPASSOU: {_P} | FALHOU: {_F}\n{'='*50}")
    sys.exit(1 if _F else 0)
