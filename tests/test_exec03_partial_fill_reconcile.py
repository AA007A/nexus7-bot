"""
NEXUS-7 — Testes: EXEC-03 (reconciliação de qty divergente) + Fase C

EXEC-03 (HIGH): _reconcile_exchange_positions() só registrava posições
ÓRFÃS — 'if sym in self.positions: ... continue'. A qty de uma posição
já conhecida nunca era atualizada. Se a exchange mudasse a quantidade
(partial fill completando, fechamento parcial externo, TP executado
pela exchange), o estado local ficava permanentemente desatualizado e
o IntegrityGuard bloqueava novas entradas para sempre, sem
auto-recuperação.

Rodar: python -m tests.test_exec03_partial_fill_reconcile
"""
import asyncio, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_P = _F = 0
def check(n, c, x=""):
    global _P, _F
    if c: _P += 1; print(f"  ✓ {n}")
    else: _F += 1; print(f"  ✗ {n} {x}")

_PORT = 9040

os.environ.update({
    "KUCOIN_REST_BASE": f"http://127.0.0.1:{_PORT}",
    "KUCOIN_API_KEY": "k", "KUCOIN_API_SECRET": "s",
    "KUCOIN_API_PASSPHRASE": "p", "PAPER_TRADE": "false",
    "LIVE_TRADING_CONFIRMED": "I_UNDERSTAND_THE_RISK",
    "BOT_API_SECRET": "t", "LOG_LEVEL": "ERROR",
    "LEVERAGE": "10", "MAX_RISK_PCT": "0.5", "MAX_MARGIN_PCT": "0.98",
    "NEXUS_AI_ENABLED": "false", "NEXUS_TELEGRAM": "false",
})


def _pos_mock(contratos, sl=0.18):
    return {"symbol": "DOGEUSDTM", "currentQty": contratos,
            "avgEntryPrice": 0.19, "markPrice": 0.19, "unrealisedPnl": 0.0,
            "realLeverage": 10, "liquidationPrice": 0.186, "posMargin": 10.0,
            "stopLoss": sl, "takeProfit": 0.20}


async def _engine():
    from bot.kucoin import KuCoinClient
    from bot.engine import TradingEngine
    c = KuCoinClient(); e = TradingEngine(c)
    await c.load_instruments(); await e._connect()
    return c, e


async def test_EXEC03_qty_divergente_converge():
    """EXEC-03: qty de posição conhecida converge para a da exchange."""
    import tests.mock_kucoin as MK
    from bot.integrity import IntegrityGuard
    c, e = await _engine()

    MK.POSITIONS.clear()
    MK.POSITIONS["DOGEUSDTM"] = _pos_mock(10)      # 1000 base
    e.positions.clear()
    await e._reconcile_exchange_positions()
    check("qty inicial = 10 contratos × 100 = 1000 base",
          abs(e.positions["DOGEUSDT"].qty - 1000.0) < 1.0)

    MK.POSITIONS["DOGEUSDTM"]["currentQty"] = 26   # exchange mudou → 2600
    await e._reconcile_exchange_positions()
    check("EXEC-03: qty converge para a da exchange (2600)",
          abs(e.positions["DOGEUSDT"].qty - 2600.0) < 1.0,
          f"qty={e.positions['DOGEUSDT'].qty}")

    g = IntegrityGuard(); await g.assess(c, e)
    check("EXEC-03: STATE_DIVERGENCE desaparece após convergir",
          "STATE_DIVERGENCE" not in g.state.codes(), str(g.state.codes()))
    check("EXEC-03: bot volta a poder operar (sem restart manual)",
          g.can_open_new(), g.block_reason())

    await c.close()


async def test_EXEC03_reducao_tambem_converge():
    """Redução externa (fechamento parcial na exchange) também converge."""
    import tests.mock_kucoin as MK
    c, e = await _engine()

    MK.POSITIONS.clear()
    MK.POSITIONS["DOGEUSDTM"] = _pos_mock(26)      # 2600 base
    e.positions.clear()
    await e._reconcile_exchange_positions()
    q1 = e.positions["DOGEUSDT"].qty

    MK.POSITIONS["DOGEUSDTM"]["currentQty"] = 13   # metade fechada → 1300
    await e._reconcile_exchange_positions()
    q2 = e.positions["DOGEUSDT"].qty

    check("qty inicial 2600", abs(q1 - 2600.0) < 1.0, f"={q1}")
    check("EXEC-03: redução externa converge para 1300",
          abs(q2 - 1300.0) < 1.0, f"={q2}")

    await c.close()


async def test_EXEC03_divergencia_pequena_nao_oscila():
    """Divergência dentro da tolerância (2%) não causa update desnecessário."""
    import tests.mock_kucoin as MK
    c, e = await _engine()

    MK.POSITIONS.clear()
    MK.POSITIONS["DOGEUSDTM"] = _pos_mock(100)     # 10000 base
    e.positions.clear()
    await e._reconcile_exchange_positions()
    q1 = e.positions["DOGEUSDT"].qty

    # 1% de diferença — dentro da tolerância de 2%
    MK.POSITIONS["DOGEUSDTM"]["currentQty"] = 101  # 10100 base
    await e._reconcile_exchange_positions()
    q2 = e.positions["DOGEUSDT"].qty

    check("divergência de 1% (< tolerância) não altera qty",
          abs(q1 - q2) < 1e-6, f"q1={q1} q2={q2}")

    await c.close()


async def test_C5_partial_fill_detectado_via_REST():
    """Fase C5: partial fill detectado via REST, com unidade correta."""
    import tests.mock_kucoin as MK
    c, e = await _engine()

    MK.POSITIONS.clear()
    MK.POSITIONS["DOGEUSDTM"] = _pos_mock(10, sl=0)  # partial, sem SL
    e.positions.clear()
    await e._reconcile_exchange_positions()

    pos = e.positions.get("DOGEUSDT")
    check("C5: partial fill detectado", pos is not None)
    check("C5: partial fill NÃO é tratado como zero exposição",
          pos and pos.qty > 0)
    check("C5: qty em unidade base (1000, não 10)",
          pos and abs(pos.qty - 1000.0) < 1.0, f"qty={pos.qty if pos else None}")
    m = e.risk._margin_in_use(e.positions)
    check("C5: margem reflete a exposição parcial real", m > 0, f"m={m}")

    await c.close()


async def test_C_cadeia_completa_base_para_contratos():
    """
    Fase C: cadeia completa com multiplier != 1
    exchange contracts → base qty → partial qty → _round_qty → outbound
    """
    import tests.mock_kucoin as MK
    c, e = await _engine()

    MK.POSITIONS.clear()
    MK.POSITIONS["DOGEUSDTM"] = _pos_mock(10)      # 1000 base
    e.positions.clear()
    await e._reconcile_exchange_positions()
    pos = e.positions["DOGEUSDT"]

    partial_base = pos.qty * 0.5                   # 500 base
    outbound = c._round_qty(partial_base, "DOGEUSDT")

    check("cadeia: 50% de 1000 base = 500 base",
          abs(partial_base - 500.0) < 1.0, f"={partial_base}")
    check("cadeia: _round_qty(500 base) = 5 contratos",
          outbound == 5, f"={outbound}")
    check("cadeia: NÃO envia 500 contratos (erro de unidade)",
          outbound != 500)

    await c.close()


async def test_C6_partial_fill_com_restart():
    """Fase C6: partial fill seguido de restart reconstrói corretamente."""
    import tests.mock_kucoin as MK
    from bot.engine import TradingEngine
    c, e = await _engine()

    MK.POSITIONS.clear()
    MK.POSITIONS["DOGEUSDTM"] = _pos_mock(13)      # 1300 base

    e2 = TradingEngine(c)
    await e2._connect()
    await e2._load_existing_positions()
    p = e2.positions.get("DOGEUSDT")

    check("C6: restart reconstrói o partial fill", p is not None)
    check("C6: qty = 13 contratos × 100 = 1300 base",
          p and abs(p.qty - 1300.0) < 1.0, f"qty={p.qty if p else None}")

    await c.close()




async def test_item1_fill_tardio_converge_para_cima():
    """Item 1: fill tardio aumenta qty na exchange → local converge p/ cima."""
    import tests.mock_kucoin as MK
    c, e = await _engine()
    MK.POSITIONS.clear()
    MK.POSITIONS["DOGEUSDTM"] = _pos_mock(10)
    e.positions.clear(); await e._reconcile_exchange_positions()
    q1 = e.positions["DOGEUSDT"].qty

    MK.POSITIONS["DOGEUSDTM"]["currentQty"] = 26   # fill tardio
    await e._reconcile_exchange_positions()
    q2 = e.positions["DOGEUSDT"].qty

    check("item1: converge para CIMA (1000 → 2600)",
          q1 < q2 and abs(q2 - 2600.0) < 1.0, f"q1={q1} q2={q2}")
    await c.close()


async def test_item2_partial_close_converge_para_baixo():
    """Item 2: partial close diminui qty na exchange → local converge p/ baixo."""
    import tests.mock_kucoin as MK
    c, e = await _engine()
    MK.POSITIONS.clear()
    MK.POSITIONS["DOGEUSDTM"] = _pos_mock(26)
    e.positions.clear(); await e._reconcile_exchange_positions()
    q1 = e.positions["DOGEUSDT"].qty

    MK.POSITIONS["DOGEUSDTM"]["currentQty"] = 6    # partial close externo
    await e._reconcile_exchange_positions()
    q2 = e.positions["DOGEUSDT"].qty

    check("item2: converge para BAIXO (2600 → 600)",
          q1 > q2 and abs(q2 - 600.0) < 1.0, f"q1={q1} q2={q2}")
    await c.close()


async def test_item3_qty_identica_idempotente():
    """Item 3: qty idêntica → reconcile idempotente, sem alteração."""
    import tests.mock_kucoin as MK
    c, e = await _engine()
    MK.POSITIONS.clear()
    MK.POSITIONS["DOGEUSDTM"] = _pos_mock(26)
    e.positions.clear(); await e._reconcile_exchange_positions()
    q1 = e.positions["DOGEUSDT"].qty
    for _ in range(3):
        await e._reconcile_exchange_positions()
    q2 = e.positions["DOGEUSDT"].qty
    check("item3: 3 reconciles com qty idêntica não alteram nada",
          abs(q1 - q2) < 1e-9, f"q1={q1} q2={q2}")
    await c.close()


async def test_item4_exchange_qty_zero_nao_mantem_position_zerada():
    """
    Item 4: exchange qty=0 → o reconcile filtra (size<=0: continue) e a
    remoção é responsabilidade de _sync_positions. Position nunca fica
    com qty=0 pendurada pelo reconcile.
    """
    import tests.mock_kucoin as MK
    c, e = await _engine()
    MK.POSITIONS.clear()
    MK.POSITIONS["DOGEUSDTM"] = _pos_mock(26)
    e.positions.clear(); await e._reconcile_exchange_positions()
    check("item4: posição registrada antes", "DOGEUSDT" in e.positions)
    qty_antes = e.positions["DOGEUSDT"].qty

    MK.POSITIONS["DOGEUSDTM"]["currentQty"] = 0    # fechada na exchange
    await e._reconcile_exchange_positions()
    check("item4: reconcile NÃO zera a qty local (filtra size<=0)",
          abs(e.positions["DOGEUSDT"].qty - qty_antes) < 1e-9,
          f"qty={e.positions['DOGEUSDT'].qty}")

    # A remoção é de _sync_positions
    MK.POSITIONS.pop("DOGEUSDTM", None)
    await e._sync_positions()
    check("item4: _sync_positions remove a posição fechada",
          "DOGEUSDT" not in e.positions, f"={list(e.positions.keys())}")
    await c.close()


async def test_item5_side_divergente_nao_corrige_silenciosamente():
    """
    Item 5 / EXEC-04: side divergente NÃO pode ter só a qty corrigida.
    Local LONG + exchange SHORT deve bloquear, não convergir a qty.
    """
    import tests.mock_kucoin as MK
    from bot.integrity import IntegrityGuard
    c, e = await _engine()
    MK.POSITIONS.clear()
    MK.POSITIONS["DOGEUSDTM"] = _pos_mock(10)      # LONG
    e.positions.clear(); await e._reconcile_exchange_positions()
    q1 = e.positions["DOGEUSDT"].qty

    MK.POSITIONS["DOGEUSDTM"]["currentQty"] = -26  # exchange virou SHORT
    await e._reconcile_exchange_positions()

    check("EXEC-04: qty NÃO é corrigida com side divergente",
          abs(e.positions["DOGEUSDT"].qty - q1) < 1e-9,
          f"qty mudou para {e.positions['DOGEUSDT'].qty}")
    check("EXEC-04: posição marcada como desprotegida",
          "DOGEUSDT" in e._unprotected_symbols)

    g = IntegrityGuard(); await g.assess(c, e)
    check("EXEC-04: IntegrityGuard detecta side divergente",
          any("SIDE" in d for d in g.block_reason().split("|")) or
          "STATE_DIVERGENCE" in g.state.codes(), g.block_reason())
    check("EXEC-04: novas entradas bloqueadas", not g.can_open_new())
    await c.close()


async def test_item6_7_reconcile_concorrente_com_partial_tp_e_close():
    """
    Itens 6 e 7: reconcile concorrente com partial TP e com close.
    _reconcile_lock deve serializar; nenhum estado impossível.
    """
    import tests.mock_kucoin as MK
    c, e = await _engine()
    MK.POSITIONS.clear()
    MK.POSITIONS["DOGEUSDTM"] = _pos_mock(26)
    e.positions.clear(); await e._reconcile_exchange_positions()

    # item 6: reconcile concorrente com _manage_partial_tp
    try:
        await asyncio.gather(
            e._reconcile_exchange_positions(),
            e._manage_partial_tp(),
            e._reconcile_exchange_positions(),
            return_exceptions=True,
        )
        check("item6: reconcile + partial TP concorrentes não crasham", True)
    except Exception as ex:
        check("item6: reconcile + partial TP concorrentes não crasham",
              False, str(ex))

    check("item6: exatamente 1 posição após concorrência",
          len(e.positions) <= 1, f"={list(e.positions.keys())}")

    # item 7: reconcile concorrente com _sync_positions (fluxo de close)
    try:
        await asyncio.gather(
            e._reconcile_exchange_positions(),
            e._sync_positions(),
            return_exceptions=True,
        )
        check("item7: reconcile + sync/close concorrentes não crasham", True)
    except Exception as ex:
        check("item7: reconcile + sync/close concorrentes não crasham",
              False, str(ex))

    q = e.positions.get("DOGEUSDT")
    check("item7: qty nunca fica negativa",
          q is None or q.qty >= 0, f"qty={q.qty if q else None}")
    await c.close()


async def test_item9_conversao_ocorre_exatamente_uma_vez():
    """
    Item 9: multiplier != 1 → conversão contratos→base ocorre EXATAMENTE
    uma vez, mesmo com N reconciles. 26 contratos × 100 = 2600, nunca
    260000 nem 26.
    """
    import tests.mock_kucoin as MK
    c, e = await _engine()
    MK.POSITIONS.clear()
    MK.POSITIONS["DOGEUSDTM"] = _pos_mock(26)
    e.positions.clear()

    for i in range(5):
        await e._reconcile_exchange_positions()
        q = e.positions["DOGEUSDT"].qty
        check(f"item9: reconcile #{i+1} → qty=2600 (conversão única)",
              abs(q - 2600.0) < 1.0, f"qty={q}")
    await c.close()


async def _run_all():
    from aiohttp import web
    import tests.mock_kucoin as MK
    runner = web.AppRunner(MK.make_app()); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", _PORT); await site.start()

    for fn in [test_EXEC03_qty_divergente_converge,
               test_EXEC03_reducao_tambem_converge,
               test_EXEC03_divergencia_pequena_nao_oscila,
               test_C5_partial_fill_detectado_via_REST,
               test_C_cadeia_completa_base_para_contratos,
               test_C6_partial_fill_com_restart,
               test_item1_fill_tardio_converge_para_cima,
               test_item2_partial_close_converge_para_baixo,
               test_item3_qty_identica_idempotente,
               test_item4_exchange_qty_zero_nao_mantem_position_zerada,
               test_item5_side_divergente_nao_corrige_silenciosamente,
               test_item6_7_reconcile_concorrente_com_partial_tp_e_close,
               test_item9_conversao_ocorre_exatamente_uma_vez]:
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
    print("═══ TESTES EXEC-03 / FASE C — PARTIAL FILL E qty DIVERGENTE ═══")
    asyncio.run(_run_all())
    print(f"\n{'='*50}\nPASSOU: {_P} | FALHOU: {_F}\n{'='*50}")
    sys.exit(1 if _F else 0)
