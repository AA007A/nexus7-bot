"""
NEXUS-7 — MODO PILOTO CONTROLADO (REAL_TRADING_PILOT)

Camada de gate ADICIONAL para a primeira validação real contra a
KuCoin Futures. Não substitui nem enfraquece nenhuma barreira
existente — soma-se a elas.

ORDEM DAS BARREIRAS (todas precisam passar):
    1. PAPER_TRADE=false + LIVE_TRADING_CONFIRMED  (bot/kucoin.py)
    2. IntegrityGuard.can_open_new()               (bot/integrity.py)
    3. viable_symbols não vazio                    (bot/engine.py)
    4. RiskManager.can_open()                      (bot/risk.py)
    5. NEXUS AI approval                           (bot/engine.py)
    6. ESTE MÓDULO — pré-condições do piloto

ATIVAÇÃO: REAL_TRADING_PILOT=true, efetivo somente fora de PAPER.
Além disso, uma abertura real no piloto exige uma autorização de release
separada da confirmação da conta:

    PILOT_RELEASE_APPROVED=I_APPROVE_ONE_LIVE_PILOT_ORDER

A ausência ou divergência desse token bloqueia a abertura real no gate
`can_open_pilot()`, que o engine executa antes de sizing e dispatch. Esta
camada permanece fail-closed mesmo depois da remoção futura do
VALIDATION_LOCK.

Sem REAL_TRADING_PILOT o módulo fica inerte: não bloqueia nem libera nada,
o comportamento é exatamente o de antes. Em PAPER ele também fica inerte,
porque as pré-condições pertencem exclusivamente ao piloto real (por exemplo
private order WS e confirmação humana da conta).
Com piloto real efetivo, aplica limites mais restritivos que a config normal:
no máximo 2 posições simultâneas e 2 submissões de novas ordens por sessão.

Este módulo NUNCA libera algo que outra barreira bloqueou. Ele só
adiciona motivos para NÃO operar no piloto real.
"""
import os
import time
import threading
from dataclasses import dataclass, field
from typing import List

from bot.logger import log


PILOT_ENABLED = os.environ.get("REAL_TRADING_PILOT", "").strip().lower() == "true"
PILOT_RELEASE_TOKEN = "I_APPROVE_ONE_LIVE_PILOT_ORDER"


def _paper_trade_enabled() -> bool:
    """Read PAPER_TRADE at decision time so runtime/test patches stay authoritative."""
    return os.environ.get("PAPER_TRADE", "true").strip().lower() == "true"


def _release_approved() -> bool:
    """A distinct, explicit release authorization for the pilot session."""
    return os.environ.get("PILOT_RELEASE_APPROVED", "").strip() == PILOT_RELEASE_TOKEN


# Limites do piloto — deliberadamente mais restritivos que a config normal.
# O piloto permite duas posições/submissões para validar diversificação sem
# transformar a primeira sessão real em exposição ilimitada.
PILOT_MAX_CONCURRENT_POSITIONS = 2
MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION = 2
PILOT_MAX_NEW_POSITIONS_SESSION = MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION

# Idade máxima aceitável do dado de mercado usado na decisão (requisito 11)
PILOT_MAX_MARKET_DATA_AGE_S = float(
    os.environ.get("PILOT_MAX_MARKET_DATA_AGE_S", "120")
)


@dataclass
class PilotState:
    """Estado do piloto — quantas ordens já foram abertas nesta sessão."""
    new_order_submissions_this_session: int = 0
    positions_opened_this_session: int = 0
    first_order_ts: float = 0.0
    blocked_reasons: List[str] = field(default_factory=list)


class PilotGuard:
    """Aplica pré-condições adicionais somente ao piloto de dinheiro real."""

    def __init__(self):
        self.state = PilotState()
        self._submission_lock = threading.Lock()
        self._last_block_log = 0.0
        self._last_block_key = ""

    @property
    def enabled(self) -> bool:
        return PILOT_ENABLED and not _paper_trade_enabled()

    def reserve_submission(self, symbol: str) -> bool:
        """Consume one of the two real-pilot session slots BEFORE sending.

        Reservations are never refunded, including on ambiguous/failed
        transport outcomes, preventing accidental duplicate submissions.
        """
        if not self.enabled:
            return True
        with self._submission_lock:
            if self.state.new_order_submissions_this_session >= MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION:
                log.warning(f"[PILOT] {symbol} submission cap reached (2)")
                return False
            self.state.new_order_submissions_this_session += 1
            self.state.first_order_ts = time.time()
        log.critical(
            f"[PILOT] symbol={symbol} submission_reserved="
            f"{self.state.new_order_submissions_this_session}/"
            f"{MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION} session"
        )
        return True

    def register_position_opened(self, symbol: str):
        """Chamado após uma abertura confirmada no piloto real."""
        if not self.enabled:
            return
        self.state.positions_opened_this_session += 1
        if self.state.first_order_ts == 0.0:
            self.state.first_order_ts = time.time()
        log.critical(
            f"🚁 [PILOT] posição aberta em {symbol} — "
            f"{self.state.positions_opened_this_session}/"
            f"{PILOT_MAX_NEW_POSITIONS_SESSION} desta sessão."
        )

    def evaluate(self, engine, client, symbol: str, ai_decision=None) -> List[str]:
        """Retorna motivos de bloqueio do piloto real; vazio significa liberado."""
        r: List[str] = []
        try:
            from bot.kucoin import API_KEY, API_SECRET, API_PASSPHRASE
            if not (API_KEY and API_SECRET and API_PASSPHRASE):
                r.append("1_AUTH: credenciais KuCoin ausentes")

            if os.environ.get("PILOT_ACCOUNT_CONFIRMED", "").strip().lower() != "true":
                r.append(
                    "2_ACCOUNT: conta real não confirmada — defina "
                    "PILOT_ACCOUNT_CONFIRMED=true após verificar que as "
                    "credenciais pertencem à conta pretendida"
                )

            if not _release_approved():
                r.append(
                    "2B_RELEASE: autorização explícita do piloto ausente; "
                    "PILOT_RELEASE_APPROVED deve corresponder ao token de release"
                )

            bal = float(getattr(engine.risk, "balance", 0) or 0)
            if bal <= 0:
                r.append(f"3_BALANCE: saldo Futures USDT = {bal}")

            if not getattr(engine, "viable_symbols", None):
                r.append("4_VIABLE: viable_symbols vazio")

            inst = getattr(engine, "instruments", None) or {}
            if not inst:
                r.append("5_INSTRUMENTS: metadata não carregada")
            elif symbol and symbol not in inst:
                r.append(f"5_INSTRUMENTS: {symbol} ausente na metadata")

            ig = getattr(engine, "integrity", None)
            if ig is not None:
                codes = ig.state.codes() if hasattr(ig, "state") else []
                if "STATE_DIVERGENCE" in codes:
                    r.append(f"6_DIVERGENCE: {ig.block_reason()[:120]}")
            else:
                r.append("6_DIVERGENCE: IntegrityGuard indisponível")
        except Exception as exc:
            r.append(f"PILOT_INTERNAL: {type(exc).__name__}")
        self.state.blocked_reasons = r
        return r
