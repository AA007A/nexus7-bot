"""Fail-closed gate for the controlled real-money pilot."""
import os
import time
import threading
from dataclasses import dataclass, field
from typing import List

from bot.logger import log

PILOT_ENABLED = os.environ.get("REAL_TRADING_PILOT", "").strip().lower() == "true"
PILOT_RELEASE_TOKEN = "I_APPROVE_TWO_LIVE_PILOT_ORDERS"


def _paper_trade_enabled() -> bool:
    return os.environ.get("PAPER_TRADE", "true").strip().lower() == "true"


def _release_approved() -> bool:
    return os.environ.get("PILOT_RELEASE_APPROVED", "").strip() == PILOT_RELEASE_TOKEN


# Controlled pilot: two concurrent positions and two new-order submissions per session.
PILOT_MAX_CONCURRENT_POSITIONS = 2
MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION = 2
PILOT_MAX_NEW_POSITIONS_SESSION = MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION
PILOT_MAX_MARKET_DATA_AGE_S = float(os.environ.get("PILOT_MAX_MARKET_DATA_AGE_S", "120"))


@dataclass
class PilotState:
    new_order_submissions_this_session: int = 0
    positions_opened_this_session: int = 0
    first_order_ts: float = 0.0
    blocked_reasons: List[str] = field(default_factory=list)


class PilotGuard:
    """Additional fail-closed gates for real pilot execution."""

    def __init__(self):
        self.state = PilotState()
        self._submission_lock = threading.Lock()
        self._last_block_log = 0.0
        self._last_block_key = ""

    @property
    def enabled(self) -> bool:
        return PILOT_ENABLED and not _paper_trade_enabled()

    def reserve_submission(self, symbol: str) -> bool:
        """Atomically reserve one of two session submission slots before dispatch."""
        if not self.enabled:
            return True
        with self._submission_lock:
            if self.state.new_order_submissions_this_session >= MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION:
                log.warning(f"[PILOT] {symbol} submission cap reached (2)")
                return False
            self.state.new_order_submissions_this_session += 1
            self.state.first_order_ts = time.time()
            reserved = self.state.new_order_submissions_this_session
        log.critical(
            f"[PILOT] symbol={symbol} submission_reserved={reserved}/"
            f"{MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION} session"
        )
        return True

    def register_position_opened(self, symbol: str):
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
        """Return pilot blockers; an empty list means the pilot gate passes."""
        r: List[str] = []
        try:
            from bot.kucoin import API_KEY, API_SECRET, API_PASSPHRASE
            if not (API_KEY and API_SECRET and API_PASSPHRASE):
                r.append("1_AUTH: credenciais KuCoin ausentes")

            if os.environ.get("PILOT_ACCOUNT_CONFIRMED", "").strip().lower() != "true":
                r.append(
                    "2_ACCOUNT: conta real não confirmada — defina "
                    "PILOT_ACCOUNT_CONFIRMED=true após verificar que as credenciais "
                    "pertencem à conta pretendida"
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

            unprot = set(getattr(engine, "_unprotected_symbols", set()) or set())
            if unprot:
                r.append(f"7_8_UNPROTECTED: {sorted(unprot)}")

            risk = getattr(engine, "risk", None)
            if risk is None or not getattr(risk, "_ready", False):
                r.append("9_RISK: RiskManager não inicializado")

            if ai_decision is None:
                r.append("10_AI: nenhuma decisão do NEXUS AI recebida")
            elif getattr(ai_decision, "execution_allowed", None) is not True:
                r.append("10_AI: NEXUS AI não aprovou a entrada")

            last_ws = float(getattr(client, "_last_ws_update", 0) or 0)
            if last_ws <= 0:
                r.append("11_MARKET_DATA: nenhum dado de mercado recebido")
            else:
                age = time.time() - last_ws
                if age > PILOT_MAX_MARKET_DATA_AGE_S:
                    r.append(
                        f"11_MARKET_DATA: dado com {age:.0f}s "
                        f"(máx {PILOT_MAX_MARKET_DATA_AGE_S:.0f}s)"
                    )

            if symbol and symbol in inst:
                meta = inst[symbol]
                missing = [k for k in ("minQty", "multiplier") if not meta.get(k)]
                if missing:
                    r.append(f"12_QTY_RULES: metadata incompleta {missing}")

            reg = getattr(engine, "orders", None)
            if reg is not None:
                try:
                    for mo in reg.pending_orders():
                        r.append(
                            f"13_AMBIGUOUS: ordem pendente {mo.client_oid[:12]} "
                            f"em {mo.symbol} (estado {mo.state.value})"
                        )
                        break
                except Exception as exc:
                    r.append(f"13_AMBIGUOUS: falha ao consultar registry: {exc}")

            if getattr(client, "_order_registry", None) is None:
                r.append("14_WS: WS privado de ordens não inicializado")

            n_pos = len(getattr(engine, "positions", {}) or {})
            if n_pos >= PILOT_MAX_CONCURRENT_POSITIONS:
                r.append(
                    f"PILOT_CONCURRENT: {n_pos} posição(ões) aberta(s), "
                    f"máx {PILOT_MAX_CONCURRENT_POSITIONS} no piloto"
                )
            if self.state.new_order_submissions_this_session >= MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION:
                r.append(
                    f"PILOT_SESSION: {self.state.new_order_submissions_this_session}/"
                    f"{PILOT_MAX_NEW_POSITIONS_SESSION} ordens já abertas nesta sessão"
                )
        except Exception as exc:
            r.append(f"PILOT_EVAL_ERROR: {type(exc).__name__}: {exc}")

        self.state.blocked_reasons = r
        return r

    def can_open_pilot(self, engine, client, symbol: str, ai_decision=None) -> bool:
        if not self.enabled:
            self.state.blocked_reasons = []
            return True
        motivos = self.evaluate(engine, client, symbol, ai_decision)
        if motivos:
            self._log_block(symbol, motivos)
            return False
        return True

    def _log_block(self, symbol: str, motivos: List[str]):
        key = f"{symbol}|{'|'.join(sorted(motivos))}"
        now = time.time()
        if key != self._last_block_key or now - self._last_block_log >= 60.0:
            self._last_block_key = key
            self._last_block_log = now
            log.warning(
                f"🚁 [PILOT] {symbol} BLOQUEADO — {len(motivos)} "
                f"pré-condição(ões) não satisfeita(s): " + " | ".join(motivos[:5])
            )

    def status(self, engine=None, client=None) -> dict:
        return {
            "pilot_configured": PILOT_ENABLED,
            "pilot_enabled": self.enabled,
            "paper_trade": _paper_trade_enabled(),
            "release_approved": _release_approved(),
            "max_concurrent_positions": PILOT_MAX_CONCURRENT_POSITIONS,
            "max_new_order_submissions_session": MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION,
            "new_order_submissions_this_session": self.state.new_order_submissions_this_session,
            "positions_opened_this_session": self.state.positions_opened_this_session,
            "blocked_reasons": list(self.state.blocked_reasons),
        }
