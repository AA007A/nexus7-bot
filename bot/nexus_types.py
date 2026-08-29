"""
NEXUS-7 — Tipos e contratos do AI Decision Engine
Seções 5, 12, 14, 18, 22 da especificação.

Define as estruturas padronizadas que atravessam todo o pipeline:
    MARKET DATA → VALIDATION → REGIME → MTF → SIGNAL
    → NEXUS AI → RISK → EXECUTION → MONITOR → FEEDBACK

Princípio (seção 14): dado inexistente NUNCA é preenchido com estimativa.
Toda ausência é registrada explicitamente como DATA_UNAVAILABLE e reduz
o data_quality, que por sua vez pode bloquear a execução.
"""
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import List, Optional, Dict, Any


# ── Decisões possíveis (seção 1) ─────────────────────────────────
class Decision(str, Enum):
    LONG      = "LONG"
    SHORT     = "SHORT"
    WAIT      = "WAIT"
    EXIT      = "EXIT"
    NO_TRADE  = "NO_TRADE"


# ── Ações de gestão de posição (seção 11) ────────────────────────
class PositionAction(str, Enum):
    HOLD         = "HOLD"
    REDUCE       = "REDUCE"
    MOVE_STOP    = "MOVE_STOP"
    TAKE_PARTIAL = "TAKE_PARTIAL"
    EXIT         = "EXIT"
    TRAIL_STOP   = "TRAIL_STOP"


# ── Regimes de mercado (seção 4) ─────────────────────────────────
class Regime(str, Enum):
    TRENDING_BULL   = "TRENDING_BULL"
    TRENDING_BEAR   = "TRENDING_BEAR"
    RANGE           = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY  = "LOW_VOLATILITY"
    BREAKOUT        = "BREAKOUT"
    BREAKDOWN       = "BREAKDOWN"
    ACCUMULATION    = "ACCUMULATION"
    DISTRIBUTION    = "DISTRIBUTION"
    CHOPPY          = "CHOPPY"
    EXTREME_EVENT   = "EXTREME_EVENT"
    UNKNOWN         = "UNKNOWN"


# ── Classificação de setup (seção 6) ─────────────────────────────
class SetupGrade(str, Enum):
    A_PLUS   = "A+"      # 90-100
    A        = "A"       # 85-89
    B        = "B"       # 75-84
    C        = "C"       # 65-74
    NO_TRADE = "NO_TRADE"  # <65

    @staticmethod
    def from_score(score: float) -> "SetupGrade":
        if score >= 90: return SetupGrade.A_PLUS
        if score >= 85: return SetupGrade.A
        if score >= 75: return SetupGrade.B
        if score >= 65: return SetupGrade.C
        return SetupGrade.NO_TRADE


# ── Divergências (seção 8) ───────────────────────────────────────
class Divergence(str, Enum):
    BULLISH        = "BULLISH_DIVERGENCE"
    BEARISH        = "BEARISH_DIVERGENCE"
    HIDDEN_BULLISH = "HIDDEN_BULLISH"
    HIDDEN_BEARISH = "HIDDEN_BEARISH"
    NONE           = "NO_DIVERGENCE"


# ── Tipo de rompimento (seção 7) ─────────────────────────────────
class BreakoutType(str, Enum):
    TRUE_BREAKOUT   = "TRUE_BREAKOUT"
    LIQUIDITY_SWEEP = "LIQUIDITY_SWEEP"
    FALSE_BREAKOUT  = "FALSE_BREAKOUT"
    NONE            = "NONE"


# ── Resultado de um modelo do ensemble (seção 17) ────────────────
@dataclass
class ModelOutput:
    """
    Saída padronizada de cada modelo independente do ensemble.

    available=False significa que o modelo não tinha dados suficientes
    (seção 14). Nesse caso ele é EXCLUÍDO da fusão em vez de contribuir
    com um valor neutro inventado.
    """
    name:       str
    direction:  Decision = Decision.WAIT
    confidence: float    = 0.0     # 0-100
    risk_score: float    = 0.0     # 0-100 (quanto maior, mais arriscado)
    reason:     str      = ""
    available:  bool     = True
    details:    dict     = field(default_factory=dict)


# ── Qualidade dos dados (seções 14 e 22) ─────────────────────────
@dataclass
class DataQuality:
    """
    Auditoria da integridade dos dados que alimentam a decisão.

    O sistema NUNCA inventa dado ausente. Cada campo indisponível é
    listado em 'unavailable' e reduz o score. Abaixo de min_quality,
    a execução é bloqueada (fail-safe).
    """
    score:        float     = 100.0
    unavailable:  List[str] = field(default_factory=list)
    stale:        List[str] = field(default_factory=list)
    errors:       List[str] = field(default_factory=list)

    def mark_unavailable(self, field_name: str, penalty: float = 10.0):
        if field_name not in self.unavailable:
            self.unavailable.append(field_name)
            self.score = max(0.0, self.score - penalty)

    def mark_stale(self, field_name: str, age_s: float, penalty: float = 15.0):
        tag = f"{field_name}({age_s:.0f}s)"
        if tag not in self.stale:
            self.stale.append(tag)
            self.score = max(0.0, self.score - penalty)

    def mark_error(self, msg: str, penalty: float = 20.0):
        self.errors.append(msg)
        self.score = max(0.0, self.score - penalty)

    @property
    def is_acceptable(self) -> bool:
        return self.score >= 60.0


# ── Decisão final (seção 18) ─────────────────────────────────────
@dataclass
class NexusDecision:
    """
    Estrutura de saída padronizada do NEXUS AI.

    execution_allowed=True é apenas a AUTORIZAÇÃO DA IA. O Risk Engine
    ainda pode vetar (seção 9) — a IA nunca tem acesso irrestrito à
    execução financeira.
    """
    symbol:            str
    decision:          str   = Decision.WAIT.value
    confidence:        float = 0.0
    setup_quality:     float = 0.0
    market_regime:     str   = Regime.UNKNOWN.value
    regime_compat:     float = 0.0
    entry:             float = 0.0
    stop_loss:         float = 0.0
    take_profit:       float = 0.0
    risk_reward:       float = 0.0
    position_size:     float = 0.0
    leverage:          int   = 0
    risk_percent:      float = 0.0
    expected_value:    float = 0.0
    time_horizon:      str   = "SHORT_TERM"
    setup_grade:       str   = SetupGrade.NO_TRADE.value
    reasoning:         List[str] = field(default_factory=list)
    warnings:          List[str] = field(default_factory=list)
    data_quality:      float = 100.0
    execution_allowed: bool  = False
    models:            List[dict] = field(default_factory=list)
    news_sentiment:    Optional[float] = None
    breakout_type:     str   = BreakoutType.NONE.value
    divergence:        str   = Divergence.NONE.value

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def wait(symbol: str, reason: str, dq: float = 100.0,
             warnings: List[str] = None) -> "NexusDecision":
        """Atalho para decisão negativa — o caminho mais comum e desejável."""
        return NexusDecision(
            symbol=symbol,
            decision=Decision.WAIT.value,
            confidence=0.0,
            execution_allowed=False,
            data_quality=dq,
            reasoning=[reason],
            warnings=warnings or [],
        )
