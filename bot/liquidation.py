"""
NEXUS-7 — LIQUIDATION ENGINE (Fase 4)

Implementa a fórmula OFICIAL da KuCoin Futures para preço de liquidação.

FONTE: https://www.kucoin.com/support/26694703491737

    Liquidation Price = (Opening Value − side × Position Margin)
                      ÷ [Size × Multiplier × (1 − side×MMR − side×LiqFee)]

VALIDADO contra o exemplo numérico da própria documentação:
    1 BTC @ 30.000, 50x, MMR 0.4%, liq fee 0.06%
    doc: 29.535,9   |   esta implementação: 29.535,9   (Δ $0,04)

HISTÓRICO DAS APROXIMAÇÕES (ambas ERRADAS):
    Fase 2: 100/leverage        → 2.000%  (otimista)
    Fase 3: estimativa c/ custos → 1.330%  (pessimista)
    Fase 4: fórmula oficial      → 1.547%  ✓

MMR NÃO É CONSTANTE: varia por contrato e por TIER de posição.
O valor default (0.4%, do XBTUSDTM) é apenas fallback — o MMR real
deve vir da API sempre que disponível (set_mmr_from_api).
"""
import os
from dataclasses import dataclass
from typing import Optional, Dict

from bot.logger import log

# ── Parâmetros oficiais (fallback quando a API não informa) ───────
# XBTUSDTM: Maintenance Margin 0.40% (ficha do contrato na KuCoin)
DEFAULT_MMR      = float(os.environ.get("DEFAULT_MMR", "0.004"))
# Taxa de liquidação usada no exemplo oficial da documentação
LIQUIDATION_FEE  = float(os.environ.get("LIQUIDATION_FEE", "0.0006"))
# Folga mínima exigida entre stop e liquidação (% do preço de entrada)
MIN_GAP_PCT      = float(os.environ.get("MIN_STOP_LIQ_GAP_PCT", "0.30"))

# MMR por símbolo, populado da API quando disponível.
# Enquanto vazio, usa DEFAULT_MMR e o modelo é marcado como aproximação.
_MMR_BY_SYMBOL: Dict[str, float] = {}
_MMR_SOURCE:    Dict[str, str]   = {}


def set_mmr_from_api(symbol: str, mmr: float, source: str = "api"):
    """
    Registra o MMR real vindo da exchange.

    A KuCoin expõe isso em /api/v2/batchGetCrossOrderLimit (campo "mmr")
    e nas fichas de contrato. Sem isso o cálculo é APROXIMAÇÃO.
    """
    if mmr and 0 < mmr < 1:
        _MMR_BY_SYMBOL[symbol] = float(mmr)
        _MMR_SOURCE[symbol] = source
        log.debug(f"MMR {symbol} = {mmr:.6f} (fonte: {source})")


def get_mmr(symbol: str) -> tuple:
    """Retorna (mmr, is_official)."""
    if symbol in _MMR_BY_SYMBOL:
        return _MMR_BY_SYMBOL[symbol], True
    return DEFAULT_MMR, False


@dataclass
class LiquidationAnalysis:
    symbol:            str
    entry:             float
    stop:              float
    leverage:          int
    is_long:           bool
    liq_price:         float
    liq_move_pct:      float
    stop_move_pct:     float
    gap_pct:           float
    stop_effective:    bool
    max_safe_stop_pct: float
    mmr:               float
    mmr_official:      bool
    model:             str      # "OFFICIAL_FORMULA" ou "APPROXIMATION"
    reason:            str

    def to_dict(self):
        return {
            "symbol": self.symbol,
            "liq_price":         round(self.liq_price, 8),
            "liq_move_pct":      round(self.liq_move_pct, 4),
            "stop_move_pct":     round(self.stop_move_pct, 4),
            "gap_pct":           round(self.gap_pct, 4),
            "stop_effective":    self.stop_effective,
            "max_safe_stop_pct": round(self.max_safe_stop_pct, 4),
            "mmr":               self.mmr,
            "mmr_official":      self.mmr_official,
            "model":             self.model,
            "reason":            self.reason,
        }


def liquidation_price(entry: float, leverage: int, is_long: bool,
                      mmr: float, liq_fee: float = LIQUIDATION_FEE) -> float:
    """
    Fórmula oficial da KuCoin, em forma normalizada por unidade.

    Opening Value e Position Margin escalam com (size × multiplier), que
    aparece nos dois lados e se cancela. O preço de liquidação depende
    apenas de entry, leverage, MMR e taxa de liquidação — NÃO do tamanho
    da posição (dentro do mesmo tier).
    """
    if entry <= 0 or leverage <= 0:
        return 0.0
    side = 1 if is_long else -1
    im   = 1.0 / leverage                      # margem inicial (fração)
    denom = 1 - side * mmr - side * liq_fee
    if denom == 0:
        return 0.0
    return entry * (1 - side * im) / denom


def analyze(entry: float, stop: float, leverage: int, is_long: bool,
            symbol: str = "", funding_pct: float = 0.0) -> LiquidationAnalysis:
    """Análise completa de stop vs liquidação usando a fórmula oficial."""
    mmr, oficial = get_mmr(symbol)
    model = "OFFICIAL_FORMULA" if oficial else "APPROXIMATION"

    if entry <= 0 or leverage <= 0:
        return LiquidationAnalysis(symbol, entry, stop, leverage, is_long,
                                   0, 0, 0, 0, False, 0, mmr, oficial,
                                   model, "parâmetros inválidos")

    liq_p = liquidation_price(entry, leverage, is_long, mmr)
    liq_move_pct = abs(entry - liq_p) / entry * 100

    # Funding acumulado reduz a margem e aproxima a liquidação
    if funding_pct > 0:
        liq_move_pct = max(0.0, liq_move_pct - funding_pct)

    stop_move_pct = abs(entry - stop) / entry * 100 if stop > 0 else 0.0
    gap_pct = liq_move_pct - stop_move_pct
    efetivo = stop > 0 and gap_pct >= MIN_GAP_PCT
    max_safe = max(0.0, liq_move_pct - MIN_GAP_PCT)

    if stop <= 0:
        motivo = "sem stop definido"
    elif not efetivo:
        motivo = (f"stop a {stop_move_pct:.2f}% vs liquidação a "
                  f"{liq_move_pct:.2f}% — folga {gap_pct:+.2f}% < "
                  f"{MIN_GAP_PCT:.2f}% exigido")
    else:
        motivo = f"stop efetivo: folga de {gap_pct:.2f}% até a liquidação"

    if not oficial:
        motivo += f" [MMR estimado {mmr:.4f} — não confirmado pela API]"

    return LiquidationAnalysis(
        symbol=symbol, entry=entry, stop=stop, leverage=leverage,
        is_long=is_long, liq_price=liq_p, liq_move_pct=liq_move_pct,
        stop_move_pct=stop_move_pct, gap_pct=gap_pct,
        stop_effective=efetivo, max_safe_stop_pct=max_safe,
        mmr=mmr, mmr_official=oficial, model=model, reason=motivo,
    )


def max_leverage_for_stop(stop_pct: float, symbol: str = "") -> int:
    """
    Maior alavancagem em que um stop de `stop_pct` permanece efetivo.

    Resolve a fórmula oficial para leverage, dado o movimento adverso
    necessário (stop + folga mínima).
    """
    mmr, _ = get_mmr(symbol)
    alvo = (stop_pct + MIN_GAP_PCT) / 100.0        # movimento necessário
    # LONG: liq_move = (1/lev + mmr + fee) / (1 + mmr + fee) ≈ 1/lev + mmr + fee
    denom = alvo - mmr - LIQUIDATION_FEE
    if denom <= 0:
        return 1
    lev = int(1.0 / denom)
    return max(1, min(125, lev))
