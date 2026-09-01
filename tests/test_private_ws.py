"""
NEXUS-7 — Testes do WebSocket privado de ordens (Fase Final).

Testa _handle_private_order_event() diretamente, sem precisar de um
servidor WS completo simulando o canal privado — a lógica de
reconciliação (Fase 8, Casos D/E/F/G) é testável isoladamente.

Rodar: python -m tests.test_private_ws
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


def test_caso_E_ws_filled_duplicado():
    """Fase 8 Caso E: WS entrega FILLED duplicado — não duplica estado."""
    from bot.kucoin import KuCoinClient
    from bot.order_state import OrderRegistry, OrderState

    async def run():
        c = KuCoinClient()
        reg = OrderRegistry()
        c._order_registry = reg
        coid = "bgx7-casoE"
        mo, _ = reg.get_or_create(coid, "BTCUSDT", "Buy", 0.01)
        mo.transition(OrderState.SUBMITTING, source="REST")
        mo.transition(OrderState.SUBMITTED, order_id="oid_E", source="REST")
        reg.index_order_id("oid_E", coid)

        evt = {"subject": "symbolOrderChange", "data": {
            "orderId": "oid_E", "clientOid": coid, "type": "match",
            "status": "done", "filledSize": "0.01", "matchSize": "0.01",
            "matchPrice": "108000", "ts": int(time.time() * 1e9),
        }}
        await c._handle_private_order_event(evt)
        e1 = mo.state
        await c._handle_private_order_event(evt)  # duplicado
        e2 = mo.state
        check("primeiro evento FILLED aplicado", e1 == OrderState.FILLED)
        check("evento duplicado não quebra nem regride estado",
              e2 == OrderState.FILLED, f"={e2}")

    asyncio.run(run())


def test_caso_F_evento_antigo_nao_sobrescreve():
    """Fase 8 Caso F: evento WS antigo não pode sobrescrever estado novo."""
    from bot.kucoin import KuCoinClient
    from bot.order_state import OrderRegistry, OrderState

    async def run():
        c = KuCoinClient()
        reg = OrderRegistry()
        c._order_registry = reg
        coid = "bgx7-casoF"
        mo, _ = reg.get_or_create(coid, "ETHUSDT", "Buy", 1.0)
        mo.transition(OrderState.SUBMITTING, source="REST")
        mo.transition(OrderState.SUBMITTED, order_id="oid_F", source="REST")
        reg.index_order_id("oid_F", coid)

        _now = time.time()
        await c._handle_private_order_event({
            "subject": "symbolOrderChange", "data": {
                "orderId": "oid_F", "clientOid": coid, "type": "match",
                "status": "done", "filledSize": "1.0", "matchSize": "1.0",
                "matchPrice": "3900", "ts": int(_now * 1e9),
            }})
        depois_novo = mo.state

        # Evento ANTIGO (5s no passado) chega depois
        await c._handle_private_order_event({
            "subject": "symbolOrderChange", "data": {
                "orderId": "oid_F", "clientOid": coid, "type": "open",
                "status": "open", "filledSize": "0",
                "ts": int((_now - 5) * 1e9),
            }})
        check("evento novo aplicado", depois_novo == OrderState.FILLED)
        check("evento antigo não sobrescreve estado mais novo",
              mo.state == OrderState.FILLED, f"={mo.state}")

    asyncio.run(run())


def test_evento_ordem_nao_rastreada():
    """Evento WS de ordem manual (não rastreada) não quebra nem é adotada."""
    from bot.kucoin import KuCoinClient
    from bot.order_state import OrderRegistry

    async def run():
        c = KuCoinClient()
        reg = OrderRegistry()
        c._order_registry = reg
        try:
            await c._handle_private_order_event({
                "subject": "symbolOrderChange", "data": {
                    "orderId": "manual_999", "clientOid": "", "type": "match",
                    "status": "done", "filledSize": "5", "matchSize": "5",
                    "matchPrice": "100", "ts": int(time.time() * 1e9),
                }})
            check("evento de ordem não rastreada não levanta exceção", True)
        except Exception as e:
            check("evento de ordem não rastreada não levanta exceção", False, str(e))
        check("ordem manual não aparece no registry",
              reg.get_by_order_id("manual_999") is None)

    asyncio.run(run())


def test_caso_G_filled_terminal_rejeita_cancelamento_tardio():
    """Fase 8 Caso G: FILLED é terminal — CANCELLED do WS não sobrescreve."""
    from bot.kucoin import KuCoinClient
    from bot.order_state import OrderRegistry, OrderState

    async def run():
        c = KuCoinClient()
        reg = OrderRegistry()
        c._order_registry = reg
        coid = "bgx7-casoG"
        mo, _ = reg.get_or_create(coid, "ETHUSDT", "Buy", 1.0)
        mo.transition(OrderState.SUBMITTING, source="REST")
        mo.transition(OrderState.SUBMITTED, order_id="oid_G", source="REST")
        mo.transition(OrderState.FILLED, filled_qty=1.0, source="REST")
        reg.index_order_id("oid_G", coid)

        try:
            await c._handle_private_order_event({
                "subject": "symbolOrderChange", "data": {
                    "orderId": "oid_G", "clientOid": coid, "type": "canceled",
                    "status": "done", "filledSize": "0",
                    "ts": int(time.time() * 1e9),
                }})
            check("evento CANCELLED pós-FILLED não crasha", True)
        except Exception as e:
            check("evento CANCELLED pós-FILLED não crasha", False, str(e))
        check("FILLED permanece — estado terminal não regride",
              mo.state == OrderState.FILLED, f"={mo.state}")

    asyncio.run(run())


def test_caso_D_sem_evento_nao_assume_filled():
    """Fase 8 Caso D: sem NENHUM evento WS, ordem não vira FILLED sozinha."""
    from bot.order_state import OrderRegistry, OrderState

    reg = OrderRegistry()
    coid = "bgx7-casoD"
    mo, _ = reg.get_or_create(coid, "BTCUSDT", "Buy", 0.01)
    mo.transition(OrderState.SUBMITTING, source="REST")
    mo.transition(OrderState.SUBMITTED, order_id="oid_D", source="REST")
    check("sem evento, permanece SUBMITTED (não assume FILLED)",
          mo.state == OrderState.SUBMITTED, f"={mo.state}")
    check("is_open ainda True — não confirmado como protegido", mo.is_open)


def test_order_registry_conectado_ao_open():
    """
    Fecha o gap encontrado na auditoria anterior: OrderRegistry era
    instanciado mas nunca alimentado dentro de _open(). Verifica por
    inspeção de código que agora está conectado.
    """
    import inspect
    from bot import engine as E
    src = inspect.getsource(E)
    i_open = src.find("async def _open(self")
    corpo = src[i_open:src.find("async def ", i_open + 10)]
    check("_open() chama self.orders.get_or_create",
          "self.orders.get_or_create" in corpo)
    check("_open() chama transition SUBMITTING",
          "OrderState.SUBMITTING" in corpo)
    check("_open() chama transition SUBMITTED",
          "OrderState.SUBMITTED" in corpo)
    check("_open() chama transition FILLED",
          "OrderState.FILLED" in corpo)
    check("_open() indexa order_id no registry",
          "index_order_id" in corpo)


def test_private_websocket_implementado():
    """Verifica existência do WS privado, sem exigir rede real."""
    from bot.kucoin import KuCoinClient
    import inspect
    src = inspect.getsource(KuCoinClient)
    check("start_private_websocket existe", "def start_private_websocket" in src)
    check("_private_ws_loop existe", "def _private_ws_loop" in src)
    check("_handle_private_order_event existe",
          "def _handle_private_order_event" in src)
    check("tópico oficial /contractMarket/tradeOrders usado",
          "/contractMarket/tradeOrders:" in src)
    check("privateChannel=True enviado (canal privado, não público)",
          '"privateChannel": True' in src)

    from bot import engine as E
    esrc = inspect.getsource(E)
    check("engine chama start_private_websocket",
          "start_private_websocket" in esrc)


if __name__ == "__main__":
    print("═══ TESTES DO WEBSOCKET PRIVADO DE ORDENS ═══")
    for fn in [test_caso_E_ws_filled_duplicado,
               test_caso_F_evento_antigo_nao_sobrescreve,
               test_evento_ordem_nao_rastreada,
               test_caso_G_filled_terminal_rejeita_cancelamento_tardio,
               test_caso_D_sem_evento_nao_assume_filled,
               test_order_registry_conectado_ao_open,
               test_private_websocket_implementado]:
        print(f"\n{fn.__name__}:")
        try:
            fn()
        except Exception as e:
            _F += 1
            import traceback
            print(f"  ✗ ERRO: {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n{chr(61)*50}\nPASSOU: {_P} | FALHOU: {_F}\n{chr(61)*50}")
    sys.exit(1 if _F else 0)
