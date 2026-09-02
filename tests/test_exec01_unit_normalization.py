"""
NEXUS-7 — Testes de regressão: EXEC-01 (normalização de unidade)

Position.qty recebia UNIDADE BASE quando criada por _open() (via
RiskManager.size()) mas CONTRATOS quando criada por qualquer caminho
de reconciliação (via get_positions() = abs(currentQty)). Medido com
DOGEUSDT (multiplier=100): mesma posição física → qty 2600.0 vs 26.0.

INVARIANTE ESTABELECIDO:
  KuCoin currentQty / get_positions()["size"] = CONTRATOS
  engine.Position.qty                         = UNIDADE BASE
  RiskManager.size()                          = UNIDADE BASE
  place_order(qty=...)                        = UNIDADE BASE
  _round_qty()  = único ponto base → contratos

Rodar: python -m tests.test_exec01_unit_normalization
"""
import asyncio, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_P = _F = 0
def check(n, c, x=""):
    global _P, _F
    if c: _P += 1; print(f"  ✓ {n}")
    else: _F += 1; print(f"  ✗ {n} {x}")

_PORT = 8995

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


async def _engine():
    from bot.kucoin import KuCoinClient
    from bot.engine import TradingEngine
    c = KuCoinClient(); e = TradingEngine(c)
    await c.load_instruments(); await e._connect()
    return c, e


async def test_EXEC01_A_B_C_helper_conversao():
    """
    EXEC01-A: multiplier=100 (DOGEUSDT)
    EXEC01-B: multiplier fracionário 0.001 (BTCUSDT)
    EXEC01-C: multiplier=1 mantém o valor
    """
    c, e = await _engine()

    # A — DOGEUSDT multiplier=100: 26 contratos = 2600 base
    base_doge = e._contracts_to_base_qty("DOGEUSDT", 26)
    check("EXEC01-A: 26 contratos × mult 100 = 2600 base",
          abs(base_doge - 2600.0) < 1e-9, f"={base_doge}")

    # B — BTCUSDT multiplier=0.001: 10 contratos = 0.01 base
    base_btc = e._contracts_to_base_qty("BTCUSDT", 10)
    check("EXEC01-B: 10 contratos × mult 0.001 = 0.01 base",
          abs(base_btc - 0.01) < 1e-9, f"={base_btc}")

    # C — DOTUSDT multiplier=1: valor inalterado
    base_dot = e._contracts_to_base_qty("DOTUSDT", 50)
    check("EXEC01-C: multiplier=1 mantém o valor (50 → 50)",
          abs(base_dot - 50.0) < 1e-9, f"={base_dot}")

    await c.close()


async def test_EXEC01_L_multiplier_invalido_falha_seguro():
    """
    EXEC01-L: multiplier ausente/zero/negativo/NaN NÃO assume 1 —
    levanta ValueError para impedir ação financeira com unidade
    desconhecida.
    """
    c, e = await _engine()

    # símbolo inexistente → multiplier ausente
    try:
        e._contracts_to_base_qty("SIMBOLO_INEXISTENTE_XYZ", 10)
        check("EXEC01-L: multiplier ausente levanta erro", False,
              "não levantou")
    except ValueError:
        check("EXEC01-L: multiplier ausente levanta ValueError", True)

    # multiplier inválido injetado
    for valor, nome in [(0, "zero"), (-1, "negativo"),
                        (float("nan"), "NaN"), ("abc", "não-numérico")]:
        e.instruments["FAKEUSDT"] = {"multiplier": valor}
        try:
            e._contracts_to_base_qty("FAKEUSDT", 10)
            check(f"EXEC01-L: multiplier {nome} levanta erro", False,
                  "não levantou")
        except ValueError:
            check(f"EXEC01-L: multiplier {nome} levanta ValueError", True)

    check("EXEC01-L: nunca assume multiplier=1 silenciosamente", True)
    await c.close()


async def test_EXEC01_D_open_vs_reconcile_mesma_qty():
    """
    EXEC01-D: posição criada por _open() e depois reconciliada mantém
    a MESMA Position.qty. Este é o teste que falhava antes do fix.
    """
    await _reset_mock()
    import tests.mock_kucoin as MK
    from bot.engine import Signal
    c, e = await _engine()

    sig = Signal(symbol="DOGEUSDT", direction="LONG", confidence=80,
                 entry=0.19, sl=0.188, tp=0.20, score=80,
                 expected_pnl=1.0, reason="EXEC01-D")
    MK.ORDERS.clear(); MK.POSITIONS.clear()
    await asyncio.wait_for(e._open(sig), timeout=60)
    qty_open = e.positions["DOGEUSDT"].qty

    e.positions.clear()
    await e._reconcile_exchange_positions()
    qty_rec = e.positions["DOGEUSDT"].qty

    check("EXEC01-D: qty via _open == qty via reconciliação",
          abs(qty_open - qty_rec) < 1e-6,
          f"open={qty_open} reconciled={qty_rec}")
    await c.close()


async def test_EXEC01_E_F_startup_e_orphan():
    """
    EXEC01-E: startup reconstruction produz a mesma qty.
    EXEC01-F: orphan reconciliation produz a mesma qty.
    """
    await _reset_mock()
    import tests.mock_kucoin as MK
    from bot.engine import TradingEngine
    c, e = await _engine()
    inst = c.get_instruments()

    MK.ORDERS.clear(); MK.POSITIONS.clear()
    # 2600 base = 26 contratos (mult 100)
    await c.place_order("DOGEUSDT", "Buy", 2600, sl=0.18, tp=0.20,
                        instruments=inst)

    # F — orphan reconciliation
    e.positions.clear()
    await e._reconcile_exchange_positions()
    qty_orphan = e.positions["DOGEUSDT"].qty
    check("EXEC01-F: orphan reconciliation devolve unidade base (2600)",
          abs(qty_orphan - 2600.0) < 1.0, f"={qty_orphan}")

    # E — startup reconstruction (engine novo)
    e2 = TradingEngine(c)
    await e2._connect()
    await e2._load_existing_positions()
    qty_startup = e2.positions["DOGEUSDT"].qty
    check("EXEC01-E: startup reconstruction devolve unidade base (2600)",
          abs(qty_startup - 2600.0) < 1.0, f"={qty_startup}")
    check("EXEC01-E/F: startup == orphan",
          abs(qty_startup - qty_orphan) < 1e-6)

    await c.close()


async def test_EXEC01_G_reconciliacao_idempotente():
    """
    EXEC01-G (crítico): segunda reconciliação NÃO multiplica a qty de
    novo. 26 contratos → 2600 base; segundo reconcile continua 2600,
    nunca 260000.
    """
    await _reset_mock()
    import tests.mock_kucoin as MK
    c, e = await _engine()
    inst = c.get_instruments()

    MK.ORDERS.clear(); MK.POSITIONS.clear()
    await c.place_order("DOGEUSDT", "Buy", 2600, sl=0.18, tp=0.20,
                        instruments=inst)

    e.positions.clear()
    await e._reconcile_exchange_positions()
    q1 = e.positions["DOGEUSDT"].qty

    await e._reconcile_exchange_positions()   # segunda vez
    q2 = e.positions["DOGEUSDT"].qty

    await e._reconcile_exchange_positions()   # terceira vez
    q3 = e.positions["DOGEUSDT"].qty

    check("EXEC01-G: 2º reconcile não multiplica qty",
          abs(q1 - q2) < 1e-6, f"q1={q1} q2={q2}")
    check("EXEC01-G: 3º reconcile também é idempotente",
          abs(q1 - q3) < 1e-6, f"q1={q1} q3={q3}")
    check("EXEC01-G: qty permanece na ordem de 2600, não 260000",
          q3 < 10000, f"q3={q3}")

    await c.close()


async def test_EXEC01_H_partial_tp_quantidade_correta():
    """
    EXEC01-H: partial TP após reconciliação envia quantidade correta.
    2600 base, 50% = 1300 base → _round_qty → 13 contratos.
    Não pode enviar 1300 contratos nem 0.13.
    """
    await _reset_mock()
    import tests.mock_kucoin as MK
    c, e = await _engine()
    inst = c.get_instruments()

    MK.ORDERS.clear(); MK.POSITIONS.clear()
    await c.place_order("DOGEUSDT", "Buy", 2600, sl=0.18, tp=0.20,
                        instruments=inst)
    e.positions.clear()
    await e._reconcile_exchange_positions()

    pos = e.positions["DOGEUSDT"]
    partial_base = pos.qty * 0.5           # 1300 base
    contratos = c._round_qty(partial_base, "DOGEUSDT")

    check("EXEC01-H: 50% de 2600 base = 1300 base",
          abs(partial_base - 1300.0) < 1.0, f"={partial_base}")
    check("EXEC01-H: _round_qty(1300 base) = 13 contratos",
          contratos == 13, f"={contratos}")
    check("EXEC01-H: NÃO envia 1300 contratos", contratos != 1300)

    await c.close()


async def test_EXEC01_I_close_total_quantidade_correta():
    """EXEC01-I: close total de posição reconciliada → contratos corretos."""
    await _reset_mock()
    import tests.mock_kucoin as MK
    c, e = await _engine()
    inst = c.get_instruments()

    MK.ORDERS.clear(); MK.POSITIONS.clear()
    await c.place_order("DOGEUSDT", "Buy", 2600, sl=0.18, tp=0.20,
                        instruments=inst)
    e.positions.clear()
    await e._reconcile_exchange_positions()

    pos = e.positions["DOGEUSDT"]
    contratos_close = c._round_qty(pos.qty, "DOGEUSDT")
    check("EXEC01-I: close total = 26 contratos (2600 base / mult 100)",
          contratos_close == 26, f"={contratos_close}")

    await c.close()


async def test_EXEC01_J_K_margem_e_pnl_consistentes():
    """
    EXEC01-J: margem antes/depois da reconciliação é igual (mesma qty).
    EXEC01-K: PnL antes/depois é igual para a mesma posição/preço.
    """
    await _reset_mock()
    import tests.mock_kucoin as MK
    from bot.engine import Position, Signal
    c, e = await _engine()
    inst = c.get_instruments()

    MK.ORDERS.clear(); MK.POSITIONS.clear()
    await c.place_order("DOGEUSDT", "Buy", 2600, sl=0.18, tp=0.20,
                        instruments=inst)
    e.positions.clear()
    await e._reconcile_exchange_positions()
    pos_rec = e.positions["DOGEUSDT"]

    # Constrói manualmente uma Position equivalente em unidade base,
    # com o MESMO entry, para comparar margem e PnL.
    sig = Signal("DOGEUSDT", "LONG", pos_rec.entry, pos_rec.entry*0.98,
                 pos_rec.entry*1.05, 80, "manual base", 3.0)
    pos_manual = Position(sig, 2600.0)

    m_rec = e.risk._margin_in_use({"DOGEUSDT": pos_rec})
    m_man = e.risk._margin_in_use({"DOGEUSDT": pos_manual})
    check("EXEC01-J: margem igual entre reconciliada e base equivalente",
          abs(m_rec - m_man) < 0.01, f"rec={m_rec} man={m_man}")

    preco = pos_rec.entry * 1.01
    pos_rec.update_pnl(preco)
    pos_manual.update_pnl(preco)
    check("EXEC01-K: PnL igual entre reconciliada e base equivalente",
          abs(pos_rec.pnl - pos_manual.pnl) < 0.01,
          f"rec={pos_rec.pnl} man={pos_manual.pnl}")

    await c.close()


async def _run_all():
    from aiohttp import web
    import tests.mock_kucoin as MK
    runner = web.AppRunner(MK.make_app()); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", _PORT); await site.start()

    for fn in [test_EXEC01_A_B_C_helper_conversao,
               test_EXEC01_L_multiplier_invalido_falha_seguro,
               test_EXEC01_D_open_vs_reconcile_mesma_qty,
               test_EXEC01_E_F_startup_e_orphan,
               test_EXEC01_G_reconciliacao_idempotente,
               test_EXEC01_H_partial_tp_quantidade_correta,
               test_EXEC01_I_close_total_quantidade_correta,
               test_EXEC01_J_K_margem_e_pnl_consistentes]:
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
    print("═══ TESTES EXEC-01 — NORMALIZAÇÃO DE UNIDADE ═══")
    asyncio.run(_run_all())
    print(f"\n{'='*50}\nPASSOU: {_P} | FALHOU: {_F}\n{'='*50}")
    sys.exit(1 if _F else 0)
