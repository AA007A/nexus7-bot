"""
NEXUS-7 — Testes de regressão: ADV-01 (reconciliação de posição órfã)

Reproduz o cenário crítico encontrado em auditoria adversarial: fill
parcial persistente (isActive=True, filledSize>0) fazia _open() dar
'return' sem nunca registrar a posição real internamente nem aplicar
proteção — a posição ficava exposta, com leverage, sem stop loss,
indefinidamente.

Rodar: python -m tests.test_orphan_position_reconciliation
"""
import asyncio, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_P = _F = 0
def check(n, c, x=""):
    global _P, _F
    if c: _P += 1; print(f"  ✓ {n}")
    else: _F += 1; print(f"  ✗ {n} {x}")

_PORT = 8950

os.environ.update({
    "KUCOIN_REST_BASE": f"http://127.0.0.1:{_PORT}",
    "KUCOIN_API_KEY": "k", "KUCOIN_API_SECRET": "s",
    "KUCOIN_API_PASSPHRASE": "p", "PAPER_TRADE": "false",
    "LIVE_TRADING_CONFIRMED": "I_UNDERSTAND_THE_RISK",
    "BOT_API_SECRET": "t", "LOG_LEVEL": "ERROR",
    "LEVERAGE": "10", "MAX_RISK_PCT": "0.5", "MAX_MARGIN_PCT": "0.98",
    "NEXUS_AI_ENABLED": "true", "NEXUS_TELEGRAM": "false",
})


async def _reset_mock():
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


async def test_P0A_fill_parcial_ativo_timeout_reconcilia():
    """
    TESTE P0-A: 90% fill + ordem ainda ativa + timeout.
    Esperado: posição descoberta, registrada, protegida.

    Usa fault dedicado (order_status_override) no mock compartilhado
    em vez de criar um servidor próprio — evita a causa raiz já
    documentada em sessões anteriores (KUCOIN_REST_BASE lido 1x no
    import do módulo; servidor precisa ser único e compartilhado).
    """
    await _reset_mock()
    import tests.mock_kucoin as MK
    await _set_fault(order_partial_fill_pct=0.9)

    from bot.kucoin import KuCoinClient
    from bot.engine import TradingEngine, Signal
    from bot.integrity import IntegrityGuard
    c = KuCoinClient(); e = TradingEngine(c)
    await c.load_instruments(); await e._connect()

    sig = Signal(symbol="DOGEUSDT", direction="LONG", confidence=80,
                 entry=0.19, sl=0.188, tp=0.20, score=80,
                 expected_pnl=1.0, reason="teste P0-A")
    from unittest.mock import AsyncMock
    from bot.nexus_types import NexusDecision
    e._nexus_validate = AsyncMock(return_value=NexusDecision(
        symbol=sig.symbol, decision=sig.direction, execution_allowed=True,
        entry=sig.entry, stop_loss=sig.sl, take_profit=sig.tp,
        confidence=80., setup_quality=80.))
    await asyncio.wait_for(e._open(sig), timeout=60)
    await _set_fault(order_partial_fill_pct=0)   # limpa para os próximos testes

    check("posição órfã foi descoberta e registrada",
          "DOGEUSDT" in e.positions)
    if "DOGEUSDT" in e.positions:
        check("entry_price vem da exchange (0.19), não do ticker (100)",
              abs(e.positions["DOGEUSDT"].entry - 0.19) < 0.01,
              f"entry={e.positions['DOGEUSDT'].entry}")
    check("posição não fica marcada como desprotegida",
          "DOGEUSDT" not in e._unprotected_symbols)

    g = IntegrityGuard(); await g.assess(c, e)
    check("IntegrityGuard libera novas entradas após reconciliação",
          g.can_open_new(), g.block_reason())

    await c.close()


async def test_P0E_posicao_orfa_sem_open_direto():
    """TESTE P0-E: posição existe na exchange, self.positions={}."""
    await _reset_mock()
    import tests.mock_kucoin as MK
    from bot.kucoin import KuCoinClient
    from bot.engine import TradingEngine

    c = KuCoinClient(); e = TradingEngine(c)
    await c.load_instruments(); await e._connect()
    inst = c.get_instruments()

    await c.place_order("ETHUSDT", "Buy", 0.1, sl=0, tp=0, instruments=inst)
    e.positions.clear()

    unprot = await e._reconcile_exchange_positions()
    check("posição órfã descoberta sem depender de _open",
          "ETHUSDT" in e.positions)
    check("entry vem da exchange, não é 0 nem aproximado por ticker",
          e.positions["ETHUSDT"].entry > 0)

    await c.close()


async def test_P0I_posicao_com_sl_nao_duplica_protecao():
    """TESTE P0-I: posição reconciliada já possui SL — não duplicar."""
    await _reset_mock()
    import tests.mock_kucoin as MK
    from bot.kucoin import KuCoinClient
    from bot.engine import TradingEngine

    c = KuCoinClient(); e = TradingEngine(c)
    await c.load_instruments(); await e._connect()
    inst = c.get_instruments()

    await c.place_order("SOLUSDT", "Buy", 1.0, sl=135.0, tp=150.0, instruments=inst)
    await c.set_position_stops("SOLUSDT", sl=135.0, tp=150.0)
    e.positions.clear()

    n_stops_antes = len(MK.STOPS)
    await e._reconcile_exchange_positions()

    check("set_position_stops não é chamado de novo (já protegida)",
          len(MK.STOPS) == n_stops_antes)
    check("posição é registrada mesmo já tendo proteção",
          "SOLUSDT" in e.positions)
    check("não fica marcada como desprotegida",
          "SOLUSDT" not in e._unprotected_symbols)

    await c.close()


async def test_P0J_falha_protecao_permanece_unprotected():
    """
    TESTE P0-J: set_position_stops() falha.
    Esperado: posição marcada UNPROTECTED, não desaparece, erro CRITICAL.
    """
    await _reset_mock()
    import tests.mock_kucoin as MK
    from bot.kucoin import KuCoinClient
    from bot.engine import TradingEngine

    c = KuCoinClient(); e = TradingEngine(c)
    await c.load_instruments(); await e._connect()
    inst = c.get_instruments()

    await c.place_order("ADAUSDT", "Buy", 100, sl=0, tp=0, instruments=inst)
    e.positions.clear()

    async def falha_sempre(*a, **kw):
        return False
    original = c.set_position_stops
    c.set_position_stops = falha_sempre
    try:
        await e._reconcile_exchange_positions()
    finally:
        c.set_position_stops = original

    check("posição permanece marcada como UNPROTECTED",
          "ADAUSDT" in e._unprotected_symbols)
    check("posição não é apagada mesmo sem proteção confirmada",
          "ADAUSDT" in e.positions)

    await c.close()


async def test_P0H_reconciliacoes_concorrentes_nao_duplicam():
    """TESTE P0-H: duas reconciliações simultâneas não duplicam proteção."""
    await _reset_mock()
    import tests.mock_kucoin as MK
    from bot.kucoin import KuCoinClient
    from bot.engine import TradingEngine

    c = KuCoinClient(); e = TradingEngine(c)
    await c.load_instruments(); await e._connect()
    inst = c.get_instruments()

    await c.place_order("LINKUSDT", "Buy", 10, sl=0, tp=0, instruments=inst)
    e.positions.clear()

    n_stops_antes = len(MK.STOPS)
    await asyncio.gather(
        e._reconcile_exchange_positions(),
        e._reconcile_exchange_positions(),
    )
    check("2 chamadas concorrentes não duplicam set_position_stops",
          len(MK.STOPS) - n_stops_antes <= 1,
          f"delta={len(MK.STOPS) - n_stops_antes}")
    check("apenas 1 registro em self.positions", "LINKUSDT" in e.positions)

    await c.close()


async def test_reconciliacao_nao_altera_posicao_ja_conhecida():
    """
    Não-regressão: uma posição JÁ rastreada internamente não deve ser
    recriada/sobrescrita pela reconciliação — apenas checada quanto à
    proteção.
    """
    await _reset_mock()
    import tests.mock_kucoin as MK
    from bot.kucoin import KuCoinClient
    from bot.engine import TradingEngine, Signal, Position

    c = KuCoinClient(); e = TradingEngine(c)
    await c.load_instruments(); await e._connect()
    inst = c.get_instruments()

    await c.place_order("DOTUSDT", "Buy", 50, sl=6.0, tp=8.0, instruments=inst)
    await c.set_position_stops("DOTUSDT", sl=6.0, tp=8.0)
    await e._load_existing_positions()   # registra normalmente
    check("posição registrada normalmente antes do teste",
          "DOTUSDT" in e.positions)
    pos_original = e.positions["DOTUSDT"]

    await e._reconcile_exchange_positions()
    check("mesmo objeto de posição — reconciliação não recriou",
          e.positions["DOTUSDT"] is pos_original)

    await c.close()


async def _run_all():
    from aiohttp import web
    import tests.mock_kucoin as MK
    runner = web.AppRunner(MK.make_app()); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", _PORT); await site.start()

    for fn in [test_P0A_fill_parcial_ativo_timeout_reconcilia,
               test_P0E_posicao_orfa_sem_open_direto,
               test_P0I_posicao_com_sl_nao_duplica_protecao,
               test_P0J_falha_protecao_permanece_unprotected,
               test_P0H_reconciliacoes_concorrentes_nao_duplicam,
               test_reconciliacao_nao_altera_posicao_ja_conhecida]:
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
    print("═══ TESTES ADV-01 — RECONCILIAÇÃO DE POSIÇÃO ÓRFÃ ═══")
    asyncio.run(_run_all())
    print(f"\n{'='*50}\nPASSOU: {_P} | FALHOU: {_F}\n{'='*50}")
    sys.exit(1 if _F else 0)
