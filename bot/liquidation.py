"""
NEXUS-7 — LIQUIDATION ENGINE (Fase 4)

Implementa a fórmula OFICIAL da KuCoin Futures para preço de liquidação.

FONTE: https://www.kucoin.com/support/26694703491737

    Liquidation Price = (Opening Value − side × Position Margin)
                      ÷ [Size × Multiplier × (1 − side×MMR − side×LiqFee)]

VALIDADO contra o exemplo numérico da própria documentação:
    1 BTC @ 30.000, 50x, MMR 0.4%, liq fee 0.06%
    doc: 29.535,9   |   esta implementação: 29.535,9   (Δ $0,04)

CONFIRMADO EM PRODUÇÃO (print de tela do usuário, ETHUSDT 50x):
    entry 2.480,84 | liq real da KuCoin: 2.440,09
    fórmula prevê: ~1,55% até liquidar | real: 1,64%
    Δ = 0,10 ponto percentual — a fórmula bate com a exchange real.

⚠️ GAP CRÍTICO — MODO DE MARGEM NÃO VERIFICADO (encontrado após a
   confirmação acima, ao notar que a conta real do usuário opera em
   CROSS MARGIN, não Isolated):

   A fórmula documentada e o exemplo numérico da KuCoin referem-se
   EXPLICITAMENTE a Isolated Margin. A própria documentação da KuCoin
   afirma que em Cross Margin:
     "In Cross Margin Mode, the max open position size is no longer
      restricted by risk limit tiers... depends on total margin
      available in the futures account, leverage, and price."
     "Margin for the same futures position = max(...) — posições
      long/short podem compartilhar/hedgear margem."

   Ou seja: em Cross, a margem de manutenção considera TODA A CONTA,
   não a posição isolada. Com UMA posição aberta (como no print), os
   dois modos tendem a convergir — e foi isso que a validação acima
   mostrou. Mas com DUAS OU MAIS posições simultâneas em Cross, este
   módulo NÃO FOI VALIDADO e pode subestimar ou superestimar a real
   distância até a liquidação, porque ele calcula cada posição de
   forma isolada.

   Esta limitação não foi identificada nas Fases 2-5 porque nenhuma
   delas verificou o modo de margem da conta configurada. Client trata
   /api/v2/changeCrossUserLeverage no comentário, mas o código nunca
   define nem lê explicitamente marginMode.

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

DEFAULT_MMR      = float(os.environ.get("DEFAULT_MMR", "0.004"))
LIQUIDATION_FEE  = float(os.environ.get("LIQUIDATION_FEE", "0.0006"))
MIN_GAP_PCT      = float(os.environ.get("MIN_STOP_LIQ_GAP_PCT", "0.30"))

_MMR_BY_SYMBOL: Dict[str, float] = {}
_MMR_SOURCE:    Dict[str, str]   = {}


def set_mmr_from_api(symbol: str, mmr: float, source: str = "api"):
    if mmr and 0 < mmr < 1:
        _MMR_BY_SYMBOL[symbol] = float(mmr)
        _MMR_SOURCE[symbol] = source
        log.debug(f"MMR {symbol} = {mmr:.6f} (fonte: {source})")


def get_mmr(symbol: str) -> tuple:
    if symbol in _MMR_BY_SYMBOL:
        return _MMR_BY_SYMBOL[symbol], True
    return DEFAULT_MMR, False


TIER1_NOTIONAL_CEILING = float(
    os.environ.get("TIER1_NOTIONAL_CEILING_USDT", "300000")
)


def notional_exceeds_tier1(notional_usdt: float) -> bool:
    return notional_usdt > TIER1_NOTIONAL_CEILING


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
    model:             str
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
    if entry <= 0 or leverage <= 0:
        return 0.0
    side = 1 if is_long else -1
    im   = 1.0 / leverage
    denom = 1 - side * mmr - side * liq_fee
    if denom == 0:
        return 0.0
    return entry * (1 - side * im) / denom


def analyze(entry: float, stop: float, leverage: int, is_long: bool,
            symbol: str = "", funding_pct: float = 0.0,
            n_open_positions: int = 1) -> LiquidationAnalysis:
    mmr, oficial = get_mmr(symbol)
    model = "OFFICIAL_FORMULA" if oficial else "APPROXIMATION"
    _cross_multi_risk = n_open_positions > 1

    if entry <= 0 or leverage <= 0:
        return LiquidationAnalysis(symbol, entry, stop, leverage, is_long,
                                   0, 0, 0, 0, False, 0, mmr, oficial,
                                   model, "parâmetros inválidos")

    liq_p = liquidation_price(entry, leverage, is_long, mmr)
    liq_move_pct = abs(entry - liq_p) / entry * 100

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

    if _cross_multi_risk:
        efetivo = False
        motivo += (
            f" [CROSS MARGIN com {n_open_positions} posições simultâneas — "
            f"cálculo de liquidação NÃO CONSIDERA margem compartilhada da "
            f"conta e não é confiável neste cenário]"
        )
        model = "UNRELIABLE_CROSS_MULTI_POSITION"

    return LiquidationAnalysis(
        symbol=symbol, entry=entry, stop=stop, leverage=leverage,
        is_long=is_long, liq_price=liq_p, liq_move_pct=liq_move_pct,
        stop_move_pct=stop_move_pct, gap_pct=gap_pct,
        stop_effective=efetivo, max_safe_stop_pct=max_safe,
        mmr=mmr, mmr_official=oficial, model=model, reason=motivo,
    )


def max_leverage_for_stop(stop_pct: float, symbol: str = "") -> int:
    """Maior alavancagem em que um stop permanece efetivo.

    Inverte exatamente a fórmula usada por ``liquidation_price`` para LONG:

        liq_move = (1/L - MMR - fee) / (1 - MMR - fee)

    Para que o stop seja efetivo, ``liq_move`` precisa ser pelo menos
    ``stop_pct + MIN_GAP_PCT``. A implementação anterior subtraía MMR/fee
    do alvo e podia reportar, por exemplo, 54x como seguro para um stop de
    2.0% quando o próprio ``analyze`` corretamente rejeitava 50x.
    """
    try:
        stop = float(stop_pct)
    except (TypeError, ValueError):
        return 1
    if stop < 0:
        return 1

    mmr, _ = get_mmr(symbol)
    target = (stop + MIN_GAP_PCT) / 100.0
    maintenance = mmr + LIQUIDATION_FEE
    denom = target * (1.0 - maintenance) + maintenance
    if denom <= 0:
        return 1
    lev = int(1.0 / denom)
    return max(1, min(125, lev))
