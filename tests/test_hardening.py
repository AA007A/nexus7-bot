"""
NEXUS-7 — Testes de hardening (Fase 3).
Rodar: python -m tests.test_hardening
"""
import asyncio, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PAPER_TRADE", "true")
os.environ.setdefault("LOG_LEVEL", "ERROR")

_P = _F = 0
def check(n, c, x=""):
    global _P, _F
    if c: _P += 1; print(f"  ✓ {n}")
    else: _F += 1; print(f"  ✗ {n} {x}")


# ══════════════════════════════════════════════════════════════════
def test_order_state_machine():
    """P0: transições inválidas devem ser IMPOSSÍVEIS."""
    from bot.order_state import ManagedOrder, OrderState, InvalidTransition

    o = ManagedOrder("oid1", "BTCUSDT", "Buy", 0.01)
    check("nasce em CREATED", o.state == OrderState.CREATED)

    o.transition(OrderState.SUBMITTING)
    o.transition(OrderState.SUBMITTED, order_id="X1")
    o.transition(OrderState.FILLED, filled_qty=0.01)
    check("fluxo feliz até FILLED", o.state == OrderState.FILLED)
    check("é terminal", o.is_terminal)

    # FILLED → SUBMITTING deve ser impossível
    try:
        o.transition(OrderState.SUBMITTING)
        check("FILLED → SUBMITTING bloqueada", False, "permitiu!")
    except InvalidTransition:
        check("FILLED → SUBMITTING bloqueada", True)

    # CREATED → FILLED (pulando etapas) deve ser impossível
    o2 = ManagedOrder("oid2", "ETHUSDT", "Buy", 1)
    try:
        o2.transition(OrderState.FILLED)
        check("CREATED → FILLED bloqueada", False, "permitiu!")
    except InvalidTransition:
        check("CREATED → FILLED bloqueada", True)

    # REJECTED é terminal
    o3 = ManagedOrder("oid3", "SOLUSDT", "Buy", 1)
    o3.transition(OrderState.SUBMITTING)
    o3.transition(OrderState.REJECTED)
    try:
        o3.transition(OrderState.SUBMITTED)
        check("REJECTED é terminal", False, "permitiu sair!")
    except InvalidTransition:
        check("REJECTED é terminal", True)

    # PARTIALLY_FILLED pode repetir (fills incrementais)
    o4 = ManagedOrder("oid4", "XRPUSDT", "Buy", 100)
    o4.transition(OrderState.SUBMITTING)
    o4.transition(OrderState.SUBMITTED)
    o4.transition(OrderState.PARTIALLY_FILLED, filled_qty=30)
    o4.transition(OrderState.PARTIALLY_FILLED, filled_qty=60)
    o4.transition(OrderState.FILLED, filled_qty=100)
    check("fills incrementais até FILLED", o4.filled_qty == 100)


def test_order_registry_idempotencia():
    """P0: 100 chamadas do MESMO sinal → 1 ordem lógica."""
    from bot.order_state import OrderRegistry

    reg = OrderRegistry()
    criadas = 0
    for _ in range(100):
        _, nova = reg.get_or_create("sinal_X", "BTCUSDT", "Buy", 0.01)
        criadas += 1 if nova else 0
    check("100 chamadas → 1 ordem criada", criadas == 1, f"={criadas}")
    check("registry tem 1 entrada", len(reg) == 1, f"={len(reg)}")


def test_liquidation_math():
    """P0: liquidação real inclui manutenção, taxas e slippage."""
    from bot.liquidation import analyze, max_leverage_for_stop

    a = analyze(100.0, 98.0, 50, True)      # SL 2% com 50x
    check("liquidação real < aproximação 100/lev",
          a.liq_move_pct < 2.0, f"={a.liq_move_pct:.2f}%")
    check("stop de 2% com 50x é INEFETIVO", not a.stop_effective)

    b = analyze(100.0, 98.0, 10, True)      # SL 2% com 10x
    check("stop de 2% com 10x é efetivo", b.stop_effective,
          f"gap={b.gap_pct:.2f}%")

    check("leverage sugerido decresce com SL maior",
          max_leverage_for_stop(1.0) > max_leverage_for_stop(3.0))

    c = analyze(100.0, 0, 10, True)
    check("sem stop → inefetivo", not c.stop_effective)


def test_integrity_fail_closed():
    """P0/P1: sem avaliação, o guard BLOQUEIA (fail-closed)."""
    from bot.integrity import IntegrityGuard

    g = IntegrityGuard()
    check("guard novo bloqueia (nunca avaliado)", not g.can_open_new())
    check("motivo é explícito", "fail-closed" in g.block_reason().lower(),
          g.block_reason())


def test_integrity_deteccao():
    """P0: cada condição crítica deve bloquear novas entradas."""
    from bot.integrity import IntegrityGuard, Severity

    class FakeClient:
        def __init__(self, **kw): self.__dict__.update(kw)
        async def get_balance(self): 
            if self._bal_raise: raise ConnectionError("REST fora do ar")
            return self._bal
        async def get_positions(self): return self._pos
        def get_instruments(self): return self._inst

    class FakeEngine:
        def __init__(self, positions=None, ready=True):
            self.positions = positions or {}
            class R: pass
            self.risk = R(); self.risk._ready = ready

    async def run():
        # REST fora do ar
        g = IntegrityGuard()
        c = FakeClient(_bal=0, _bal_raise=True, _pos=[], _inst={"X":{}},
                       _time_offset_ms=0, _last_ws_update=time.time())
        await g.assess(c, FakeEngine())
        check("REST indisponível bloqueia", not g.can_open_new())
        check("código REST_UNAVAILABLE presente",
              "REST_UNAVAILABLE" in g.state.codes(), g.state.codes())

        # Posição sem stop na exchange
        g2 = IntegrityGuard()
        c2 = FakeClient(_bal=100, _bal_raise=False,
                        _pos=[{"symbol":"BTCUSDT","size":1,"stopLoss":0,
                               "entryPrice":100}],
                        _inst={"BTCUSDT":{}}, _time_offset_ms=0,
                        _last_ws_update=time.time())
        await g2.assess(c2, FakeEngine())
        check("posição sem stop bloqueia", not g2.can_open_new())
        check("código POSITION_WITHOUT_STOP presente",
              "POSITION_WITHOUT_STOP" in g2.state.codes(), g2.state.codes())

        # Divergência: exchange tem posição, local não
        g3 = IntegrityGuard()
        c3 = FakeClient(_bal=100, _bal_raise=False,
                        _pos=[{"symbol":"ETHUSDT","size":1,"stopLoss":95,
                               "entryPrice":100}],
                        _inst={"ETHUSDT":{}}, _time_offset_ms=0,
                        _last_ws_update=time.time())
        await g3.assess(c3, FakeEngine(positions={}))
        check("posição órfã bloqueia", not g3.can_open_new())
        check("código STATE_DIVERGENCE presente",
              "STATE_DIVERGENCE" in g3.state.codes(), g3.state.codes())

        # Relógio dessincronizado
        g4 = IntegrityGuard()
        c4 = FakeClient(_bal=100, _bal_raise=False, _pos=[],
                        _inst={"X":{}}, _time_offset_ms=99999,
                        _last_ws_update=time.time())
        await g4.assess(c4, FakeEngine())
        check("clock skew bloqueia", not g4.can_open_new())

        # Risk Engine não inicializado
        g5 = IntegrityGuard()
        c5 = FakeClient(_bal=100, _bal_raise=False, _pos=[],
                        _inst={"X":{}}, _time_offset_ms=0,
                        _last_ws_update=time.time())
        await g5.assess(c5, FakeEngine(ready=False))
        check("Risk Engine indisponível bloqueia", not g5.can_open_new())

        # Tudo OK → libera
        g6 = IntegrityGuard()
        await g6.assess(c5, FakeEngine(ready=True))
        check("estado saudável LIBERA entradas", g6.can_open_new(),
              g6.block_reason())

    asyncio.run(run())


def test_integrity_nao_bloqueia_gestao():
    """P0: o kill switch NÃO deve impedir gestão de posições abertas."""
    import ast, inspect
    from bot import engine as E
    src = inspect.getsource(E)
    # a chamada de gestão roda sob _pos_lock, ANTES do can_open_new
    i_lock = src.find("async with self._pos_lock")
    i_kill = src.find("integrity.can_open_new")
    check("gestão de posições vem ANTES do kill switch",
          i_lock != -1 and i_kill != -1 and i_lock < i_kill,
          f"lock={i_lock} kill={i_kill}")


if __name__ == "__main__":
    print("═══ TESTES DE HARDENING (Fase 3) ═══")
    for fn in [test_order_state_machine, test_order_registry_idempotencia,
               test_liquidation_math, test_integrity_fail_closed,
               test_integrity_deteccao, test_integrity_nao_bloqueia_gestao]:
        print(f"\n{fn.__name__}:")
        try:
            fn()
        except Exception as e:
            _F += 1
            import traceback
            print(f"  ✗ ERRO: {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n{'═'*50}\nPASSOU: {_P} | FALHOU: {_F}\n{'═'*50}")
    sys.exit(1 if _F else 0)
