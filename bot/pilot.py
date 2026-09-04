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
    6. ESTE MÓDULO — 14 pré-condições do piloto

ATIVAÇÃO: REAL_TRADING_PILOT=true, efetivo somente fora de PAPER.

Sem essa variável o módulo fica inerte: não bloqueia nem libera nada,
o comportamento é exatamente o de antes. Em PAPER ele também fica
inerte, porque as 14 pré-condições pertencem exclusivamente ao piloto
real (por exemplo private order WS e confirmação humana da conta).
Com piloto real efetivo, aplica limites mais restritivos que o normal
(1 posição, 1 ordem por sessão).

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


def _paper_trade_enabled() -> bool:
    """Read PAPER_TRADE at decision time so runtime/test patches stay authoritative."""
    return os.environ.get("PAPER_TRADE", "true").strip().lower() == "true"


# Limites do piloto — deliberadamente mais restritivos que a config normal
PILOT_MAX_CONCURRENT_POSITIONS = 1
MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION = 1
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
    """Aplica as 14 pré-condições somente ao piloto de dinheiro real."""

    def __init__(self):
        self.state = PilotState()
        self._submission_lock = threading.Lock()
        self._last_block_log = 0.0
        self._last_block_key = ""

    @property
    def enabled(self) -> bool:
        # REAL_TRADING_PILOT is a real-money safety layer. Applying its
        # private-order-WS/account-confirmation requirements to PAPER makes a
        # valid simulated candidate impossible to open and defeats paper E2E.
        return PILOT_ENABLED and not _paper_trade_enabled()

    def reserve_submission(self, symbol: str) -> bool:
        """Consume the real-pilot session BEFORE sending; never refunded."""
        if not self.enabled:
            return True
        with self._submission_lock:
            if self.state.new_order_submissions_this_session >= MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION:
                log.warning(f"[PILOT] {symbol} second submission blocked")
                return False
            self.state.new_order_submissions_this_session += 1
            self.state.first_order_ts = time.time()
        log.critical(f"[PILOT] symbol={symbol} submission_reserved=1 session_consumed=true")
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
            f"{PILOT_MAX_NEW_POSITIONS_SESSION} desta sessão. "
            f"Nenhuma nova entrada até o ciclo E2E ser encerrado e "
            f"reconciliado."
        )

    def evaluate(self, engine, client, symbol: str, ai_decision=None) -> List[str]:
        """Retorna motivos de bloqueio do piloto real; vazio significa liberado."""
        r: List[str] = []
        try:
            # 1. API autenticada — credenciais presentes
            from bot.kucoin import API_KEY, API_SECRET, API_PASSPHRASE
            if not (API_KEY and API_SECRET and API_PASSPHRASE):
                r.append("1_AUTH: credenciais KuCoin ausentes")

            # 2. Ambiente confirmado como a conta real pretendida
            if os.environ.get("PILOT_ACCOUNT_CONFIRMED", "").strip().lower() != "true":
                r.append(
                    "2_ACCOUNT: conta real não confirmada — defina "
                    "PILOT_ACCOUNT_CONFIRMED=true após verificar que as "
                    "credenciais pertencem à conta pretendida"
                )

            # 3. Saldo Futures USDT > 0
            bal = float(getattr(engine.risk, "balance", 0) or 0)
            if bal <= 0:
                r.append(f"3_BALANCE: saldo Futures USDT = {bal}")

            # 4. viable_symbols não vazio
            if not getattr(engine, "viable_symbols", None):
                r.append("4_VIABLE: viable_symbols vazio")

            # 5. Instrument metadata carregado
            inst = getattr(engine, "instruments", None) or {}
            if not inst:
                r.append("5_INSTRUMENTS: metadata não carregada")
            elif symbol and symbol not in inst:
                r.append(f"5_INSTRUMENTS: {symbol} ausente na metadata")

            # 6. Nenhum STATE_DIVERGENCE ativo
            ig = getattr(engine, "integrity", None)
            if ig is not None:
                codes = ig.state.codes() if hasattr(ig, "state") else []
                if "STATE_DIVERGENCE" in codes:
                    r.append(f"6_DIVERGENCE: {ig.block_reason()[:120]}")
            else:
                r.append("6_DIVERGENCE: IntegrityGuard indisponível")

            # 7/8. Posição órfã não reconciliada / símbolos desprotegidos
            unprot = set(getattr(engine, "_unprotected_symbols", set()) or set())
            if unprot:
                r.append(f"7_8_UNPROTECTED: {sorted(unprot)}")

            # 9. RiskManager ativo
            risk = getattr(engine, "risk", None)
            if risk is None or not getattr(risk, "_ready", False):
                r.append("9_RISK: RiskManager não inicializado")

            # 10. NEXUS AI executado e aprovando
            if ai_decision is None:
                r.append("10_AI: nenhuma decisão do NEXUS AI recebida")
            elif getattr(ai_decision, "execution_allowed", None) is not True:
                r.append("10_AI: NEXUS AI não aprovou a entrada")

            # 11. Market data recente
            last_ws = float(getattr(client, "_last_ws_update", 0) or 0)
            if last_ws <= 0:
                r.append("11_MARKET_DATA: nenhum dado de mercado recebido")
            else:
                idade = time.time() - last_ws
                if idade > PILOT_MAX_MARKET_DATA_AGE_S:
                    r.append(
                        f"11_MARKET_DATA: dado com {idade:.0f}s "
                        f"(máx {PILOT_MAX_MARKET_DATA_AGE_S:.0f}s)"
                    )

            # 12. Quantidade respeitando regras da exchange — validado no _open()
            if symbol and symbol in inst:
                meta = inst[symbol]
                faltando = [k for k in ("minQty", "multiplier") if not meta.get(k)]
                if faltando:
                    r.append(f"12_QTY_RULES: metadata incompleta {faltando}")

            # 13. Nenhuma submission pendente, em QUALQUER símbolo
            reg = getattr(engine, "orders", None)
            if reg is not None and symbol:
                try:
                    for mo in reg.pending_orders():
                        r.append(
                            f"13_AMBIGUOUS: ordem pendente {mo.client_oid[:12]} "
                            f"em {mo.symbol} (estado {mo.state.value})"
                        )
                        break
                except Exception as e:
                    r.append(f"13_AMBIGUOUS: falha ao consultar registry: {e}")

            # 14. Private WS ou mecanismo equivalente ativo — requisito real only
            if getattr(client, "_order_registry", None) is None:
                r.append("14_WS: WS privado de ordens não inicializado")

            # Limites do piloto real
            n_pos = len(getattr(engine, "positions", {}) or {})
            if n_pos >= PILOT_MAX_CONCURRENT_POSITIONS:
                r.append(
                    f"PILOT_CONCURRENT: {n_pos} posição(ões) aberta(s), "
                    f"máx {PILOT_MAX_CONCURRENT_POSITIONS} no piloto"
                )
            if self.state.new_order_submissions_this_session >= MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION:
                r.append(
                    f"PILOT_SESSION: {self.state.new_order_submissions_this_session}"
                    f"/{PILOT_MAX_NEW_POSITIONS_SESSION} ordens já abertas "
                    f"nesta sessão — ciclo E2E precisa ser encerrado e "
                    f"reconciliado antes de outra entrada"
                )

        except Exception as e:
            r.append(f"PILOT_EVAL_ERROR: {type(e).__name__}: {e}")

        self.state.blocked_reasons = r
        return r

    def can_open_pilot(self, engine, client, symbol: str, ai_decision=None) -> bool:
        """True outside an effective real pilot; otherwise all 14 gates must pass."""
        if not self.enabled:
            # Clear stale real-pilot diagnostics so PAPER status cannot claim it
            # is blocked by a gate that is intentionally inapplicable.
            self.state.blocked_reasons = []
            return True

        motivos = self.evaluate(engine, client, symbol, ai_decision)
        if motivos:
            self._log_block(symbol, motivos)
            return False
        return True

    def _log_block(self, symbol: str, motivos: List[str]):
        """Log com throttle — não repete o mesmo bloqueio a cada ciclo."""
        key = f"{symbol}|{'|'.join(sorted(motivos))}"
        agora = time.time()
        if key != self._last_block_key or agora - self._last_block_log >= 60.0:
            self._last_block_key = key
            self._last_block_log = agora
            log.warning(
                f"🚁 [PILOT] {symbol} BLOQUEADO — {len(motivos)} "
                f"pré-condição(ões) não satisfeita(s): " + " | ".join(motivos[:5])
            )

    def status(self, engine=None, client=None) -> dict:
        """Snapshot para /health e relatórios."""
        return {
            "pilot_configured": PILOT_ENABLED,
            "pilot_enabled": self.enabled,
            "paper_trade": _paper_trade_enabled(),
            "max_concurrent_positions": PILOT_MAX_CONCURRENT_POSITIONS,
            "max_new_order_submissions_session": MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION,
            "new_order_submissions_this_session": self.state.new_order_submissions_this_session,
            "positions_opened_this_session": self.state.positions_opened_this_session,
            "blocked_reasons": list(self.state.blocked_reasons),
        }
