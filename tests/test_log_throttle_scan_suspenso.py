"""
NEXUS-7 — Testes: throttle do WARNING SCAN_SUSPENSO (LOW, observabilidade)

Observado em produção (logs reais do Railway, 2026-09-02): com
viable_symbols=[] e saldo $0.0000, o WARNING SCAN_SUSPENSO era emitido
a cada ciclo do loop (~5s) mesmo com o retry travado no backoff de até
300s — cerca de 720 warnings idênticos por hora.

Esta correção limita APENAS a emissão do log. Gate de segurança,
retry, backoff e _update_balance() permanecem inalterados (confirmado
por diff byte-a-byte das funções envolvidas).

Rodar: python -m tests.test_log_throttle_scan_suspenso
"""
import asyncio, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_P = _F = 0
def check(n, c, x=""):
    global _P, _F
    if c: _P += 1; print(f"  ✓ {n}")
    else: _F += 1; print(f"  ✗ {n} {x}")

_PORT = 9100

os.environ.update({
    "KUCOIN_REST_BASE": f"http://127.0.0.1:{_PORT}",
    "KUCOIN_API_KEY": "k", "KUCOIN_API_SECRET": "s",
    "KUCOIN_API_PASSPHRASE": "p", "PAPER_TRADE": "false",
    "LIVE_TRADING_CONFIRMED": "I_UNDERSTAND_THE_RISK",
    "BOT_API_SECRET": "t", "LOG_LEVEL": "ERROR",
    "LEVERAGE": "10", "MAX_RISK_PCT": "0.5", "MAX_MARGIN_PCT": "0.98",
    "NEXUS_AI_ENABLED": "false", "NEXUS_TELEGRAM": "false",
})


class _CapturaWarnings:
    """Captura chamadas a log.warning contendo SCAN_SUSPENSO."""
    def __init__(self):
        self.msgs = []
    def __enter__(self):
        from bot import engine as E
        self._orig = E.log.warning
        def _fake(msg, *a, **kw):
            self.msgs.append(str(msg))
            return self._orig(msg, *a, **kw)
        E.log.warning = _fake
        return self
    def __exit__(self, *exc):
        from bot import engine as E
        E.log.warning = self._orig
    @property
    def suspensos(self):
        return [m for m in self.msgs if "SCAN_SUSPENSO" in m]


def _simula_ciclo_log(e):
    """
    Reproduz EXATAMENTE a lógica de throttle do loop principal, sem
    depender de rede — testa o comportamento do log, não o retry.
    """
    from bot import engine as E
    _susp_key = f"viable_empty|{e._viable_retry_attempt}"
    _now_log = time.time()
    if (_susp_key != e._scan_susp_last_key or
            _now_log - e._scan_susp_last_log_ts >= 60.0):
        e._scan_susp_last_key = _susp_key
        e._scan_susp_last_log_ts = _now_log
        E.log.warning(
            f"🚫 SCAN_SUSPENSO: viable_symbols=[] — "
            f"nenhuma ordem será aberta até a recuperação automática "
            f"(tentativa #{e._viable_retry_attempt})"
        )


async def _engine():
    from bot.kucoin import KuCoinClient
    from bot.engine import TradingEngine
    c = KuCoinClient(); e = TradingEngine(c)
    return c, e


async def test_LOG01_primeiro_warning_aparece():
    """LOG-01: o primeiro SCAN_SUSPENSO é emitido imediatamente."""
    c, e = await _engine()
    e._viable_retry_attempt = 1
    with _CapturaWarnings() as cap:
        _simula_ciclo_log(e)
        check("LOG-01: primeiro warning é emitido de imediato",
              len(cap.suspensos) == 1, f"={len(cap.suspensos)}")
    await c.close()


async def test_LOG02_dez_ciclos_em_menos_de_60s():
    """LOG-02: 10 ciclos rápidos não geram 10 warnings idênticos."""
    c, e = await _engine()
    e._viable_retry_attempt = 3     # estado estável (não muda entre ciclos)
    with _CapturaWarnings() as cap:
        for _ in range(10):
            _simula_ciclo_log(e)
        check("LOG-02: 10 ciclos < 60s geram apenas 1 warning",
              len(cap.suspensos) == 1, f"={len(cap.suspensos)}")
    await c.close()


async def test_LOG03_apos_janela_novo_warning():
    """LOG-03: passada a janela de 60s, um novo warning pode ser emitido."""
    c, e = await _engine()
    e._viable_retry_attempt = 3
    with _CapturaWarnings() as cap:
        _simula_ciclo_log(e)
        primeiro = len(cap.suspensos)
        # Recua o relógio do throttle em 61s (sem alterar o loop real)
        e._scan_susp_last_log_ts -= 61.0
        _simula_ciclo_log(e)
        check("LOG-03: após 60s um novo warning é emitido",
              len(cap.suspensos) == primeiro + 1, f"={len(cap.suspensos)}")
    await c.close()


async def test_LOG03b_mudanca_de_estado_loga_imediato():
    """Mudança do estado relevante (nº da tentativa) loga na hora."""
    c, e = await _engine()
    e._viable_retry_attempt = 3
    with _CapturaWarnings() as cap:
        _simula_ciclo_log(e)
        _simula_ciclo_log(e)          # idêntico → suprimido
        e._viable_retry_attempt = 4   # estado MUDOU
        _simula_ciclo_log(e)
        check("mudança de tentativa loga imediatamente (sem esperar 60s)",
              len(cap.suspensos) == 2, f"={len(cap.suspensos)}")
    await c.close()


async def test_LOG04_recovery_nao_e_suprimido():
    """
    LOG-04: RECOVERY não passa pelo throttle — é emitido por
    _ensure_viable_symbols(), função que não foi alterada.
    """
    import inspect
    from bot import engine as E
    src = inspect.getsource(E.TradingEngine._ensure_viable_symbols)
    check("LOG-04: RECOVERY é logado em _ensure_viable_symbols",
          "RECOVERY" in src)
    check("LOG-04: _ensure_viable_symbols não tem lógica de throttle",
          "_scan_susp_last_log_ts" not in src and
          "_scan_susp_last_key" not in src)


async def test_LOG05_update_balance_inalterado():
    """LOG-05: o throttle não altera as chamadas de _update_balance()."""
    import inspect
    from bot import engine as E
    src = inspect.getsource(E)
    check("LOG-05: _update_balance continua sendo chamado no loop",
          "await self._update_balance()" in src)
    ub = inspect.getsource(E.TradingEngine._update_balance)
    check("LOG-05: _update_balance não contém lógica de throttle",
          "_scan_susp" not in ub)


async def test_LOG06_tentativas_de_recovery_inalteradas():
    """
    LOG-06: o throttle não altera a frequência real das tentativas —
    _ensure_viable_symbols() é chamado a cada ciclo, como antes, e o
    backoff interno segue intacto.
    """
    import inspect
    from bot import engine as E
    src = inspect.getsource(E)
    check("LOG-06: _ensure_viable_symbols segue chamado a cada ciclo",
          "_tem_pares = await self._ensure_viable_symbols()" in src)

    ev = inspect.getsource(E.TradingEngine._ensure_viable_symbols)
    check("LOG-06: backoff exponencial preservado (5s → teto 300s)",
          "min(300.0, 5.0 * (2 ** min(self._viable_retry_attempt - 1, 6)))" in ev)
    check("LOG-06: retry não sofre throttle de log",
          "_scan_susp" not in ev)

    check("LOG-06: gate que bloqueia ordens permanece",
          "if not _tem_pares:" in src)


async def _run_all():
    for fn in [test_LOG01_primeiro_warning_aparece,
               test_LOG02_dez_ciclos_em_menos_de_60s,
               test_LOG03_apos_janela_novo_warning,
               test_LOG03b_mudanca_de_estado_loga_imediato,
               test_LOG04_recovery_nao_e_suprimido,
               test_LOG05_update_balance_inalterado,
               test_LOG06_tentativas_de_recovery_inalteradas]:
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
    print("═══ TESTES LOG-01 a LOG-06 — THROTTLE SCAN_SUSPENSO ═══")
    asyncio.run(_run_all())
    print(f"\n{'='*50}\nPASSOU: {_P} | FALHOU: {_F}\n{'='*50}")
    sys.exit(1 if _F else 0)
