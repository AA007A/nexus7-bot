"""
NEXUS-7 — INTEGRITY GUARD (Fase 3, P0)

Barreira única e explícita contra novas entradas quando o estado da
exchange não pode ser confirmado.

PRINCÍPIO: EXCHANGE = SOURCE OF TRUTH.
Qualquer divergência entre estado local e exchange bloqueia NOVAS
ENTRADAS — nunca abandona o gerenciamento de posições existentes.

FAIL-CLOSED: na dúvida, bloqueia. A ausência de informação nunca é
tratada como "está tudo bem".

Uso:
    guard = IntegrityGuard()
    await guard.assess(client, engine)      # avalia e registra
    if not guard.can_open_new():
        return                              # bloqueado, com motivo
"""
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from bot.logger import log


class Severity(str, Enum):
    OK       = "OK"
    DEGRADED = "DEGRADED"   # opera, mas com ressalva
    BLOCKED  = "BLOCKED"    # não abre novas posições


# ── TTL máximo por tipo de dado (P1 — dados stale) ────────────────
# Valores proporcionais à natureza de cada dado. Preço muda a cada
# segundo; funding, a cada 8 horas.
TTL = {
    "balance":    float(os.environ.get("TTL_BALANCE",    "120")),
    "positions":  float(os.environ.get("TTL_POSITIONS",  "120")),
    "price":      float(os.environ.get("TTL_PRICE",       "60")),
    "candles":    float(os.environ.get("TTL_CANDLES",   "1800")),
    "instruments":float(os.environ.get("TTL_INSTRUMENTS","86400")),
    "clock":      float(os.environ.get("TTL_CLOCK",     "3600")),
}

# Desvio máximo tolerado entre relógio local e o da exchange
MAX_CLOCK_SKEW_MS = float(os.environ.get("MAX_CLOCK_SKEW_MS", "5000"))


@dataclass
class IntegrityIssue:
    code:     str
    severity: Severity
    detail:   str
    ts:       float = field(default_factory=time.time)


@dataclass
class IntegrityState:
    """Snapshot da última avaliação."""
    severity:      Severity = Severity.OK
    issues:        List[IntegrityIssue] = field(default_factory=list)
    checked_at:    float = 0.0
    exchange_known: bool = False

    @property
    def blocked(self) -> bool:
        return self.severity == Severity.BLOCKED

    def codes(self) -> List[str]:
        return [i.code for i in self.issues]


class IntegrityGuard:
    """
    Avalia a integridade do estado e decide se novas entradas são
    permitidas. Não executa ordens nem altera posições.
    """

    def __init__(self):
        self.state = IntegrityState()
        self._last_block_log = 0.0
        self._consec_failures = 0

    # ── Avaliação ────────────────────────────────────────────────
    async def assess(self, client, engine) -> IntegrityState:
        """
        Executa todas as verificações e atualiza self.state.

        NUNCA levanta exceção: uma falha na própria verificação é, ela
        mesma, motivo para bloquear (fail-closed).
        """
        issues: List[IntegrityIssue] = []
        exchange_known = True

        def add(code, sev, detail):
            issues.append(IntegrityIssue(code, sev, detail))

        # 1. REST disponível + saldo confirmado
        try:
            bal = await client.get_balance()
            if bal is None or bal < 0:
                add("BALANCE_UNCONFIRMED", Severity.BLOCKED,
                    f"saldo não confirmado (retorno={bal})")
                exchange_known = False
            elif bal == 0:
                add("BALANCE_ZERO", Severity.BLOCKED, "saldo zero")
        except Exception as e:
            add("REST_UNAVAILABLE", Severity.BLOCKED,
                f"REST indisponível: {type(e).__name__}: {e}")
            exchange_known = False

        # 2. Posições confirmadas na exchange
        ex_positions = None
        try:
            ex_positions = await client.get_positions()
            if ex_positions is None:
                add("POSITIONS_UNCONFIRMED", Severity.BLOCKED,
                    "get_positions retornou None")
                exchange_known = False
        except Exception as e:
            add("POSITIONS_UNCONFIRMED", Severity.BLOCKED,
                f"posições não confirmadas: {type(e).__name__}: {e}")
            exchange_known = False

        # 3. Reconciliação local ↔ exchange (EXCHANGE = SOURCE OF TRUTH)
        if ex_positions is not None:
            div = self._reconcile(engine, ex_positions)
            for d in div:
                add("STATE_DIVERGENCE", Severity.BLOCKED, d)

        # 4. INVARIANTE DE STOP LOSS:
        #    POSITION_OPEN → PROTECTIVE_STOP_CONFIRMED
        if ex_positions:
            for p in ex_positions:
                try:
                    if abs(float(p.get("size", 0) or 0)) <= 0:
                        continue
                    sl = float(p.get("stopLoss", 0) or 0)
                    if sl <= 0:
                        add("POSITION_WITHOUT_STOP", Severity.BLOCKED,
                            f"{p.get('symbol')} aberta SEM stop confirmado "
                            f"na exchange")
                except Exception as e:
                    add("POSITION_UNREADABLE", Severity.BLOCKED, str(e))

        # 5. Instrumentos sincronizados
        try:
            inst = client.get_instruments()
            if not inst:
                add("INSTRUMENTS_MISSING", Severity.BLOCKED,
                    "nenhum instrumento carregado")
                exchange_known = False
        except Exception as e:
            add("INSTRUMENTS_MISSING", Severity.BLOCKED, str(e))
            exchange_known = False

        # 6. Relógio sincronizado com a exchange
        skew = getattr(client, "_time_offset_ms", None)
        if skew is None:
            add("CLOCK_UNSYNCED", Severity.DEGRADED,
                "offset de relógio desconhecido")
        elif abs(skew) > MAX_CLOCK_SKEW_MS:
            add("CLOCK_SKEW", Severity.BLOCKED,
                f"desvio de relógio {skew:.0f}ms > {MAX_CLOCK_SKEW_MS:.0f}ms")

        # 7. WebSocket / frescor dos dados de mercado
        try:
            last_ws = getattr(client, "_last_ws_update", 0) or 0
            if last_ws:
                age = time.time() - last_ws
                if age > TTL["candles"]:
                    add("MARKET_DATA_STALE", Severity.BLOCKED,
                        f"último dado de mercado há {age/60:.1f}min")
                elif age > TTL["price"] * 5:
                    add("WS_LAGGING", Severity.DEGRADED,
                        f"WS atrasado {age:.0f}s")
            else:
                # Sem WS, o bot ainda opera por REST — degradado, não bloqueado
                add("WS_NEVER_CONNECTED", Severity.DEGRADED,
                    "WebSocket nunca entregou dados")
        except Exception as e:
            add("WS_UNKNOWN", Severity.DEGRADED, str(e))

        # 8. Risk Engine disponível e inicializado
        try:
            risk = getattr(engine, "risk", None)
            if risk is None or not getattr(risk, "_ready", False):
                add("RISK_ENGINE_UNAVAILABLE", Severity.BLOCKED,
                    "Risk Engine não inicializado")
        except Exception as e:
            add("RISK_ENGINE_UNAVAILABLE", Severity.BLOCKED, str(e))

        # 9. Rate limit persistente
        n429 = getattr(client, "_rate_limit_hits", 0)
        if n429 >= int(os.environ.get("RATE_LIMIT_BLOCK_AFTER", "5")):
            add("RATE_LIMITED", Severity.BLOCKED,
                f"{n429} respostas 429 recentes")

        # ── Consolidação ─────────────────────────────────────────
        if any(i.severity == Severity.BLOCKED for i in issues):
            sev = Severity.BLOCKED
        elif any(i.severity == Severity.DEGRADED for i in issues):
            sev = Severity.DEGRADED
        else:
            sev = Severity.OK

        self.state = IntegrityState(
            severity=sev, issues=issues,
            checked_at=time.time(), exchange_known=exchange_known,
        )
        self._log_state()
        return self.state

    # ── Reconciliação ────────────────────────────────────────────
    def _reconcile(self, engine, ex_positions: list) -> List[str]:
        """
        Compara estado local com a exchange. A exchange é a autoridade.
        Retorna a lista de divergências encontradas.
        """
        div = []
        try:
            ex = {}
            for p in ex_positions:
                sym = p.get("symbol")
                sz  = abs(float(p.get("size", 0) or 0))
                if sym and sz > 0:
                    ex[sym] = p

            local = dict(getattr(engine, "positions", {}) or {})

            # Local tem, exchange não → posição fantasma
            for sym in local:
                if sym not in ex:
                    div.append(
                        f"{sym}: registrada localmente mas INEXISTENTE na "
                        f"exchange (posição fantasma)"
                    )

            # Exchange tem, local não → posição órfã
            for sym in ex:
                if sym not in local:
                    div.append(
                        f"{sym}: existe na exchange mas NÃO rastreada "
                        f"localmente (posição órfã)"
                    )

            # Ambos têm → comparar quantidade e entrada
            _tol_qty   = float(os.environ.get("RECON_QTY_TOL",   "0.02"))
            _tol_price = float(os.environ.get("RECON_PRICE_TOL", "0.01"))
            for sym in set(local) & set(ex):
                lp, xp = local[sym], ex[sym]
                lq = abs(float(getattr(lp, "qty", 0) or 0))
                xq = abs(float(xp.get("size", 0) or 0))
                if lq > 0 and xq > 0 and abs(lq - xq) / max(lq, xq) > _tol_qty:
                    div.append(f"{sym}: qty local {lq} ≠ exchange {xq}")

                le = float(getattr(lp, "entry", 0) or 0)
                xe = float(xp.get("entryPrice", 0) or 0)
                if le > 0 and xe > 0 and abs(le - xe) / xe > _tol_price:
                    div.append(
                        f"{sym}: entry local {le:.6f} ≠ exchange {xe:.6f}"
                    )
        except Exception as e:
            div.append(f"falha ao reconciliar: {type(e).__name__}: {e}")
        return div

    # ── Decisão ──────────────────────────────────────────────────
    def can_open_new(self) -> bool:
        """
        Única fonte de verdade sobre permissão de NOVAS ENTRADAS.
        Gerenciamento de posições existentes NÃO passa por aqui.
        """
        # Fail-closed: sem avaliação recente, bloqueia.
        if self.state.checked_at <= 0:
            return False
        idade = time.time() - self.state.checked_at
        if idade > float(os.environ.get("INTEGRITY_MAX_AGE", "300")):
            return False
        return not self.state.blocked

    def block_reason(self) -> str:
        if self.state.checked_at <= 0:
            return "integridade nunca avaliada (fail-closed)"
        idade = time.time() - self.state.checked_at
        if idade > float(os.environ.get("INTEGRITY_MAX_AGE", "300")):
            return f"avaliação de integridade obsoleta ({idade:.0f}s)"
        blk = [i for i in self.state.issues if i.severity == Severity.BLOCKED]
        if not blk:
            return ""
        return " | ".join(f"{i.code}: {i.detail}" for i in blk[:4])

    def _log_state(self):
        s = self.state
        if s.severity == Severity.BLOCKED:
            now = time.time()
            if now - self._last_block_log > 60:
                self._last_block_log = now
                log.error(
                    f"🚫 INTEGRIDADE: NOVAS ENTRADAS BLOQUEADAS — "
                    f"{self.block_reason()}"
                )
        elif s.severity == Severity.DEGRADED:
            log.debug(f"⚠️ Integridade degradada: {', '.join(s.codes())}")

    def to_dict(self) -> dict:
        s = self.state
        return {
            "severity":        s.severity.value,
            "can_open_new":    self.can_open_new(),
            "block_reason":    self.block_reason(),
            "exchange_known":  s.exchange_known,
            "checked_at":      s.checked_at,
            "age_seconds":     round(time.time() - s.checked_at, 1) if s.checked_at else None,
            "issues": [
                {"code": i.code, "severity": i.severity.value, "detail": i.detail}
                for i in s.issues
            ],
        }
