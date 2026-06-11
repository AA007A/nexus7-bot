"""
BGX Capital — Daily Tracker v1.0 (extraído de engine.py)
v12: Splitting do engine.py em submódulos para melhor manutenibilidade.

Responsabilidades:
  - Reset diário de PnL às 00:00 UTC
  - Controle de meta diária (DAILY_TARGET_PCT)
  - Controle de stop-loss diário (DAILY_STOP_LOSS_PCT)
  - Recálculo de limites baseado no saldo real
  - Modo agressivo → conservador após atingir meta
"""
from bot.config import cfg
from bot.logger import log


class DailyTracker:
    """
    Rastreia PnL diário e controla os limites de meta e stop.
    Instanciado pelo TradingEngine e atualizado a cada trade fechado.
    """

    def __init__(self):
        self.daily_pnl        = 0.0
        self.daily_target     = cfg.DAILY_TARGET      # fallback USD
        self.daily_stop_loss  = cfg.DAILY_STOP_LOSS   # fallback USD
        self.daily_target_hit = False
        self.daily_stopped    = False
        self._last_reset_day  = -1

    def recalc_limits(self, balance: float):
        """
        Recalcula meta e stop diários em % do saldo real.
        Chamado após cada atualização de saldo e no reset diário.
        """
        if balance > 0:
            self.daily_target    = balance * cfg.DAILY_TARGET_PCT
            self.daily_stop_loss = balance * cfg.DAILY_STOP_LOSS_PCT

    def check_reset(self, balance: float):
        """
        Reset diário às 00:00 UTC. Recalcula limites com saldo atual.
        """
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).day
        if today != self._last_reset_day:
            self._last_reset_day  = today
            self.daily_pnl        = 0.0
            self.daily_target_hit = False
            self.daily_stopped    = False
            self.recalc_limits(balance)
            log.info(
                f"🌅 Reset diário — Meta: ${self.daily_target:.2f} "
                f"({cfg.DAILY_TARGET_PCT * 100:.1f}% saldo) | "
                f"Stop: -${self.daily_stop_loss:.2f}"
            )

    def add_pnl(self, pnl_net: float):
        """Registra PnL de um trade fechado."""
        self.daily_pnl += pnl_net

    def check_limits(self) -> str:
        """
        Verifica se a meta ou stop diário foi atingido.
        Retorna: 'TARGET', 'STOP', ou 'OK'
        """
        if self.daily_pnl >= self.daily_target and not self.daily_target_hit:
            self.daily_target_hit = True
            log.info(
                f"🎯 Meta diária atingida! PnL=${self.daily_pnl:.2f} "
                f"≥ ${self.daily_target:.2f} → modo conservador"
            )
            return 'TARGET'
        if self.daily_pnl <= -self.daily_stop_loss and not self.daily_stopped:
            self.daily_stopped = True
            log.warning(
                f"🛑 Stop diário ativado! PnL=${self.daily_pnl:.2f} "
                f"≤ -${self.daily_stop_loss:.2f} → sem novas entradas"
            )
            return 'STOP'
        return 'OK'

    def can_trade(self) -> bool:
        """True se o bot pode abrir novas posições hoje."""
        return not self.daily_stopped

    def effective_score_threshold(self) -> int:
        """Score mínimo efetivo: mais alto após meta (modo conservador)."""
        return cfg.POST_TARGET_SCORE if self.daily_target_hit else cfg.MIN_ENTRY_SCORE

    def effective_risk_mult(self) -> float:
        """Multiplicador de risco: reduzido após meta diária."""
        return cfg.POST_TARGET_RISK / cfg.MAX_RISK_PCT if self.daily_target_hit else 1.0

    def to_dict(self) -> dict:
        return {
            "daily_pnl":       round(self.daily_pnl, 4),
            "daily_target":    round(self.daily_target, 2),
            "daily_stop_loss": round(self.daily_stop_loss, 2),
            "target_hit":      self.daily_target_hit,
            "stopped":         self.daily_stopped,
            "can_trade":       self.can_trade(),
            "mode":            (
                "CONSERVADOR" if self.daily_target_hit
                else "PARADO" if self.daily_stopped
                else "AGRESSIVO"
            ),
        }
