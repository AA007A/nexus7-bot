"""
NEXUS-7 — ORDER STATE MACHINE (Fase 3, P0)

Modela explicitamente o ciclo de vida de uma ordem e PROÍBE transições
inválidas. Sem isso, um retry podia reverter uma ordem já preenchida
para "enviando", abrindo caminho para duplicação.

    CREATED → SUBMITTING → SUBMITTED → PARTIALLY_FILLED → FILLED
                    ↓           ↓              ↓
                 FAILED    REJECTED       CANCELLED

Estados terminais (FILLED, REJECTED, CANCELLED, FAILED) não admitem
saída — uma vez lá, a ordem não volta atrás.
"""
import time
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List

from bot.logger import log


class OrderState(str, Enum):
    CREATED         = "CREATED"
    SUBMITTING      = "SUBMITTING"
    SUBMITTED       = "SUBMITTED"
    PARTIALLY_FILLED= "PARTIALLY_FILLED"
    FILLED          = "FILLED"
    REJECTED        = "REJECTED"
    CANCELLED       = "CANCELLED"
    FAILED          = "FAILED"


TERMINAIS = {OrderState.FILLED, OrderState.REJECTED,
             OrderState.CANCELLED, OrderState.FAILED}

# Transições permitidas. Tudo que não estiver aqui é inválido.
TRANSICOES = {
    OrderState.CREATED:          {OrderState.SUBMITTING, OrderState.FAILED},
    OrderState.SUBMITTING:       {OrderState.SUBMITTED, OrderState.REJECTED,
                                  OrderState.FAILED},
    OrderState.SUBMITTED:        {OrderState.PARTIALLY_FILLED, OrderState.FILLED,
                                  OrderState.REJECTED, OrderState.CANCELLED},
    OrderState.PARTIALLY_FILLED: {OrderState.PARTIALLY_FILLED, OrderState.FILLED,
                                  OrderState.CANCELLED},
    OrderState.FILLED:           set(),
    OrderState.REJECTED:         set(),
    OrderState.CANCELLED:        set(),
    OrderState.FAILED:           set(),
}


class InvalidTransition(Exception):
    """Transição proibida — indica bug de lógica, não erro operacional."""
    pass


@dataclass
class ManagedOrder:
    """
    Ordem com ciclo de vida controlado.

    client_oid é a chave de idempotência: duas ManagedOrder com o mesmo
    client_oid representam a MESMA ordem lógica, mesmo após restart.
    """
    client_oid: str
    symbol:     str
    side:       str
    qty:        float
    state:      OrderState = OrderState.CREATED
    order_id:   Optional[str] = None
    filled_qty: float = 0.0
    avg_price:  float = 0.0      # preço médio real de execução (dealValue/dealSize)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)   # última transição
    last_source: str  = ""       # "REST" ou "WS" — de onde veio a última atualização
    history:    List[tuple] = field(default_factory=list)

    def transition(self, novo: OrderState, **info):
        """
        Aplica uma transição. Levanta InvalidTransition se proibida.
        """
        if novo == self.state and novo != OrderState.PARTIALLY_FILLED:
            return   # idempotente: mesmo estado não é erro

        permitidos = TRANSICOES.get(self.state, set())
        if novo not in permitidos:
            raise InvalidTransition(
                f"{self.symbol} [{self.client_oid[:8]}]: "
                f"{self.state.value} → {novo.value} é INVÁLIDA "
                f"(permitidas: {sorted(x.value for x in permitidos) or 'nenhuma (terminal)'})"
            )

        anterior = self.state
        self.state = novo
        self.updated_at = time.time()
        self.last_source = info.get("source", self.last_source)
        self.history.append((time.time(), anterior.value, novo.value, info))

        if "order_id" in info and info["order_id"]:
            self.order_id = info["order_id"]
        if "filled_qty" in info:
            self.filled_qty = float(info["filled_qty"])
        if "avg_price" in info and info["avg_price"]:
            self.avg_price = float(info["avg_price"])

        log.debug(
            f"📋 {self.symbol} [{self.client_oid[:8]}]: "
            f"{anterior.value} → {novo.value}"
        )

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAIS

    @property
    def is_open(self) -> bool:
        """Ordem viva na exchange (pode ainda executar)."""
        return self.state in (OrderState.SUBMITTED, OrderState.PARTIALLY_FILLED)

    def to_dict(self) -> dict:
        return {
            "client_oid": self.client_oid, "symbol": self.symbol,
            "side": self.side, "qty": self.qty, "state": self.state.value,
            "order_id": self.order_id, "filled_qty": self.filled_qty,
            "terminal": self.is_terminal,
            "transitions": len(self.history),
        }


class OrderRegistry:
    """
    Registro de ordens por client_oid — garante idempotência mesmo com
    múltiplos workers ou após restart.

    Também indexado por order_id (Fase 2: correlação primária quando
    ambos orderId e clientOid estão disponíveis, ex: eventos do WS
    privado que trazem só orderId).
    """

    def __init__(self):
        self._orders: dict = {}
        self._by_order_id: dict = {}   # order_id -> client_oid

    def get_or_create(self, client_oid: str, symbol: str,
                      side: str, qty: float) -> tuple:
        """
        Retorna (ordem, criada_agora).

        Se já existe ordem com este client_oid, devolve a existente —
        é o mesmo sinal lógico, não uma nova ordem.
        """
        if client_oid in self._orders:
            return self._orders[client_oid], False
        o = ManagedOrder(client_oid=client_oid, symbol=symbol,
                         side=side, qty=qty)
        self._orders[client_oid] = o
        return o, True

    def get(self, client_oid: str) -> Optional[ManagedOrder]:
        return self._orders.get(client_oid)

    def get_by_order_id(self, order_id: str) -> Optional[ManagedOrder]:
        """Correlação primária por orderId (Fase 2) — usado pelo WS
        privado, que identifica eventos por orderId, não client_oid."""
        coid = self._by_order_id.get(order_id)
        return self._orders.get(coid) if coid else None

    def index_order_id(self, order_id: str, client_oid: str):
        """Registra o vínculo order_id -> client_oid assim que a
        exchange retorna o orderId (dentro de place_order)."""
        if order_id and client_oid:
            self._by_order_id[order_id] = client_oid

    def open_orders(self, symbol: str = None) -> List[ManagedOrder]:
        return [o for o in self._orders.values()
                if o.is_open and (symbol is None or o.symbol == symbol)]

    def gc(self, max_age: float = 86400):
        """Remove ordens terminais antigas."""
        agora = time.time()
        for k in [k for k, o in list(self._orders.items())
                  if o.is_terminal and agora - o.created_at > max_age]:
            self._orders.pop(k, None)

    def __len__(self):
        return len(self._orders)
