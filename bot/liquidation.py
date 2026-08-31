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

# ══════════════════════════════════════════════════════════════════
# TIERS DE MMR (Fase 5C) — CONFIRMADO na documentação oficial
#
# FONTE: kucoin.com/support/26685810193433 (Risk Limit Levels)
#        + fichas de contrato (kucoin.com/futures/contract/detail/*)
#
# "with the BTC perpetual contract (USDT)... Position Size = 300,000
#  USDT... this would fall under Level 1, where the MMR is 0.4%"
#
# CONFIRMADO: Tier 1 do XBTUSDTM tem MMR = 0.40%. Isso bate com o
# DEFAULT_MMR usado desde a Fase 4 — não era um chute, mas eu não tinha
# a fonte direta até agora.
#
# NÃO CONFIRMADO: o MMR SOBE por tier conforme o valor da posição
# aumenta ("the maintenance margin rate is 0.5%" em outro exemplo da
# doc, para posição maior). Os limiares exatos de cada tier e a tabela
# completa por símbolo exigem o endpoint /api/v1/contracts/risk-limit,
# que está bloqueado neste ambiente (Fase 5A).
#
# Por isso o Tier 1 (0.4%) é usado como fallback SEMPRE que a API não
# responder — é o cenário mais comum (posições pequenas) e o valor tem
# fonte primária confirmada. Posições GRANDES terão MMR real maior que
# este fallback, subestimando o risco de liquidação nesse caso
# específico. set_mmr_from_api() deve sobrepor assim que disponível.
# ══════════════════════════════════════════════════════════════════
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


# Acima deste notional, o Tier 1 (0.4%) pode não ser mais válido — a
# doc cita 300.000 USDT como exemplo do limite do Tier 1 do BTC.
# Fallback conservador: alerta, não bloqueia (não temos a tabela exata).
TIER1_NOTIONAL_CEILING = float(
    os.environ.get("TIER1_NOTIONAL_CEILING_USDT", "300000")
)


def notional_exceeds_tier1(notional_usdt: float) -> bool:
    """
    True se o notional pode ter saído do Tier 1 assumido pelo fallback.

    Isso NÃO é uma tabela de tiers real — é um teto conservador citado
    no exemplo oficial da KuCoin para o BTC. Serve para avisar, não
    para decidir com precisão.
    """
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
            symbol: str = "", funding_pct: float = 0.0,
            n_open_positions: int = 1) -> LiquidationAnalysis:
    """
    Análise completa de stop vs liquidação usando a fórmula oficial.

    n_open_positions: quantas posições estão abertas SIMULTANEAMENTE na
    conta. Confirmado em produção (print de tela real) que a conta
    opera em CROSS MARGIN. A fórmula foi validada apenas para o caso de
    posição isolada / única posição em cross (que convergem quando há
    apenas uma posição). Com 2+ posições em cross, a margem de
    manutenção real depende da conta inteira — este módulo NÃO calcula
    isso, e o resultado é marcado como não confiável.
    """
    mmr, oficial = get_mmr(symbol)
    model = "OFFICIAL_FORMULA" if oficial else "APPROXIMATION"
    _cross_multi_risk = n_open_positions > 1

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

    if _cross_multi_risk:
        efetivo = False   # não confia no gap calculado — força cautela
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
