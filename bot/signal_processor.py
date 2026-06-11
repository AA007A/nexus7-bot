"""
BGX Capital — Signal Processor v1.0 (extraído de engine.py)
v12: Splitting do engine.py em submódulos para melhor manutenibilidade.

Responsabilidades:
  - Filtragem de símbolos viáveis (_filter_viable_symbols)
  - Scan de todos os pares e geração de sinais (_scan_all_and_enter)
  - Avaliação de score e aprovação de entrada
  - Integração com: strategy, score, filters, correlation, regime
"""
from bot.logger import log
from bot.config import cfg


class SignalProcessorMixin:
    """
    Mixin que adiciona lógica de processamento de sinais ao TradingEngine.
    Importado em engine.py: class TradingEngine(SignalProcessorMixin, PositionManagerMixin):

    Oferece acesso a métodos de scan sem duplicar código no engine principal.
    Os métodos reais permanecem no engine.py por ora (para manter compatibilidade).
    Esta classe serve de namespace e documentação — migração incremental.
    """

    def effective_score_threshold(self) -> int:
        """Score mínimo efetivo: mais alto após meta diária."""
        return (
            cfg.POST_TARGET_SCORE
            if self.daily_tracker.daily_target_hit
            else cfg.MIN_ENTRY_SCORE
        )

    def effective_risk_mult(self) -> float:
        """Multiplicador de risco após meta diária."""
        if self.daily_tracker.daily_target_hit:
            return cfg.POST_TARGET_RISK / cfg.MAX_RISK_PCT
        return 1.0

    def is_viable_symbol(self, symbol: str, volume_24h: float,
                          min_vol_usdt: float = 50_000_000) -> bool:
        """
        Verifica se um símbolo tem volume suficiente para operar.
        Critério: volume 24h > 50M USDT (configurável).
        """
        return volume_24h >= min_vol_usdt

    def log_scan_summary(self, scanned: int, signals: int, opened: int):
        """Log de resumo do ciclo de scan."""
        log.info(
            f"📊 Scan: {scanned} pares | {signals} sinais | "
            f"{opened} abertas | posições={len(self.positions)}/{cfg.MAX_POSITIONS}"
        )
