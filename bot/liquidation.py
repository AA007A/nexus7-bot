"""
NEXUS-7 — LIQUIDATION MATH (Fase 3, P0)

Calcula o preço real de liquidação e determina se um stop loss é
efetivo — isto é, se ele encerra a posição ANTES de a exchange
liquidá-la.

A Fase 2 usava uma aproximação (100/leverage). Isso ignora:
  • margem de manutenção (a exchange liquida ANTES de zerar a margem)
  • taxas de entrada e saída
  • funding acumulado
  • slippage na execução do stop

Todos empurram a liquidação para MAIS PERTO do preço de entrada.

REGRA: o stop deve ficar numa região que não dependa da liquidação
para encerrar a posição. Se não for possível garantir, REJEITAR.
"""
import os
from dataclasses import dataclass

# Margem de manutenção da KuCoin Futures (varia por tier de posição).
# 0.5% é o valor típico para posições pequenas; conservador o bastante.
MAINT_MARGIN_RATE = float(os.environ.get("MAINT_MARGIN_RATE", "0.005"))
TAKER_FEE         = float(os.environ.get("TAKER_FEE", "0.0006"))
SLIPPAGE          = float(os.environ.get("EXEC_SLIPPAGE", "0.0005"))
# Folga mínima exigida entre stop e liquidação, em % do preço
MIN_GAP_PCT       = float(os.environ.get("MIN_STOP_LIQ_GAP_PCT", "0.30"))


@dataclass
class LiquidationAnalysis:
    entry:            float
    stop:             float
    leverage: int
    is_long:          bool
    liq_price:        float
    liq_move_pct:     float   # % de movimento adverso até liquidar
    stop_move_pct:    float   # % de movimento adverso até o stop
    gap_pct:          float   # folga entre stop e liquidação
    stop_effective:   bool    # o stop dispara antes da liquidação?
    max_safe_stop_pct: float  # maior stop que ainda é efetivo
    reason:           str

    def to_dict(self):
        return {
            "liq_price":        round(self.liq_price, 8),
            "liq_move_pct":     round(self.liq_move_pct, 4),
            "stop_move_pct":    round(self.stop_move_pct, 4),
            "gap_pct":          round(self.gap_pct, 4),
            "stop_effective":   self.stop_effective,
            "max_safe_stop_pct":round(self.max_safe_stop_pct, 4),
            "reason":           self.reason,
        }


def analyze(entry: float, stop: float, leverage: int, is_long: bool,
            funding_pct: float = 0.0) -> LiquidationAnalysis:
    """
    Análise completa de stop vs liquidação, com custos incluídos.

    O movimento até a liquidação é MENOR que 1/leverage porque:
      - a exchange liquida ao atingir a margem de manutenção
      - taxas de abertura já consumiram parte da margem
      - funding acumulado reduz a margem disponível
    """
    if entry <= 0 or leverage <= 0:
        return LiquidationAnalysis(entry, stop, leverage, is_long, 0, 0, 0, 0,
                                   False, 0, "parâmetros inválidos")

    # Margem inicial como fração do notional
    im = 1.0 / leverage

    # Custos que corroem a margem antes de qualquer movimento de preço
    custo = TAKER_FEE * 2 + SLIPPAGE + max(0.0, funding_pct)

    # Movimento adverso até a liquidação:
    #   margem_inicial - movimento - custos = margem_manutenção
    liq_move = im - MAINT_MARGIN_RATE - custo
    liq_move = max(0.0, liq_move)
    liq_move_pct = liq_move * 100

    liq_price = entry * (1 - liq_move) if is_long else entry * (1 + liq_move)

    # Movimento até o stop
    stop_move_pct = abs(entry - stop) / entry * 100 if stop > 0 else 0.0

    gap_pct = liq_move_pct - stop_move_pct
    efetivo = stop > 0 and gap_pct >= MIN_GAP_PCT

    # Maior stop que ainda mantém a folga exigida
    max_safe = max(0.0, liq_move_pct - MIN_GAP_PCT)

    if stop <= 0:
        motivo = "sem stop definido"
    elif not efetivo:
        motivo = (f"stop a {stop_move_pct:.2f}% vs liquidação a "
                  f"{liq_move_pct:.2f}% — folga {gap_pct:.2f}% < "
                  f"{MIN_GAP_PCT:.2f}% exigido")
    else:
        motivo = (f"stop efetivo: folga de {gap_pct:.2f}% até a liquidação")

    return LiquidationAnalysis(
        entry=entry, stop=stop, leverage=leverage, is_long=is_long,
        liq_price=liq_price, liq_move_pct=liq_move_pct,
        stop_move_pct=stop_move_pct, gap_pct=gap_pct,
        stop_effective=efetivo, max_safe_stop_pct=max_safe, reason=motivo,
    )


def max_leverage_for_stop(stop_pct: float) -> int:
    """
    Maior alavancagem em que um stop de `stop_pct` ainda é efetivo.

    Responde diretamente: "com SL de 2.27%, qual leverage posso usar?"
    """
    custo = TAKER_FEE * 2 + SLIPPAGE
    alvo  = (stop_pct + MIN_GAP_PCT) / 100.0 + MAINT_MARGIN_RATE + custo
    if alvo <= 0:
        return 125
    lev = int(1.0 / alvo)
    return max(1, min(125, lev))
