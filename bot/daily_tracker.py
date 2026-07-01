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
    Rastreia PnL diário, semanal e mensal.
    Controla meta/stop com limites em camadas.

    Camadas de proteção:
      Diário:  meta=1% saldo, stop=1% saldo
      Semanal: stop=3% saldo
      Mensal:  stop=8% saldo
    """

    def __init__(self):
        self.daily_pnl        = 0.0
        self.weekly_pnl       = 0.0   # acumulado na semana
        self.monthly_pnl      = 0.0   # acumulado no mês
        self.daily_target     = cfg.DAILY_TARGET      # fallback USD
        self.daily_stop_loss  = cfg.DAILY_STOP_LOSS   # fallback USD
        self.weekly_stop_loss = 0.0   # calculado em recalc_limits
        self.monthly_stop_loss= 0.0   # calculado em recalc_limits
        self.daily_target_hit = False
        self.daily_stopped    = False
        self.weekly_stopped   = False
        self.monthly_stopped  = False
        self._last_reset_day  = -1
        self._last_reset_week = -1
        self._last_reset_month= -1
        # Journal para análise por tipo/sessão/regime
        self.trade_journal: list = []

    def recalc_limits(self, balance: float):
        """
        Recalcula meta e stop diários/semanais/mensais em % do saldo real.
        MELHORIA: stop diário 0.5% → 1%, + semanal 3%, + mensal 8%
        """
        if balance > 0:
            self.daily_target     = round(balance * cfg.DAILY_TARGET_PCT, 2)
            # FIX: mínimo absoluto $1.00 — com saldos pequenos (<$100) o stop
            # percentual fica tão pequeno que qualquer taxa de exchange ativa
            raw_daily_stop        = balance * cfg.DAILY_STOP_LOSS_PCT
            self.daily_stop_loss  = max(round(raw_daily_stop, 2), 1.00)
            self.weekly_stop_loss = max(round(balance * getattr(cfg, "WEEKLY_STOP_PCT",  0.03), 2), 3.00)
            self.monthly_stop_loss= max(round(balance * getattr(cfg, "MONTHLY_STOP_PCT", 0.08), 2), 8.00)

    def check_reset(self, balance: float):
        """
        Reset diário/semanal/mensal às 00:00 UTC.
        Recalcula limites com saldo atual em cada reset.
        """
        from datetime import datetime, timezone
        now   = datetime.now(timezone.utc)
        today = now.day
        week  = now.isocalendar()[1]
        month = now.month

        # Reset mensal
        if month != self._last_reset_month:
            self._last_reset_month = month
            self.monthly_pnl       = 0.0
            self.monthly_stopped   = False
            self.recalc_limits(balance)
            log.info(f"📅 Reset mensal — Stop mensal: -${self.monthly_stop_loss:.2f}")

        # Reset semanal
        if week != self._last_reset_week:
            self._last_reset_week = week
            self.weekly_pnl       = 0.0
            self.weekly_stopped   = False
            log.info(f"📅 Reset semanal — Stop semanal: -${self.weekly_stop_loss:.2f}")

        # Reset diário
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

    def add_pnl(self, pnl_net: float,
                symbol: str = "",
                entry_type: str = "",
                regime: str = "",
                session: str = "",
                rr_achieved: float = 0.0):
        """
        Registra PnL de um trade fechado.
        Acumula diário/semanal/mensal e adiciona ao journal para análise.
        """
        self.daily_pnl   += pnl_net
        self.weekly_pnl  += pnl_net
        self.monthly_pnl += pnl_net

        # Journal estruturado para análise por tipo/sessão/regime
        from datetime import datetime, timezone
        self.trade_journal.append({
            "ts":         datetime.now(timezone.utc).isoformat(),
            "symbol":     symbol,
            "pnl":        round(pnl_net, 4),
            "entry_type": entry_type,
            "regime":     regime,
            "session":    session,
            "rr":         round(rr_achieved, 2),
            "win":        pnl_net > 0,
        })
        # Manter journal com no máximo 500 trades em memória
        if len(self.trade_journal) > 500:
            self.trade_journal = self.trade_journal[-500:]

    def check_limits(self) -> str:
        """
        Verifica limites em 3 camadas: mensal, semanal, diario.
        Retorna: 'TARGET','MONTHLY_STOP','WEEKLY_STOP','STOP','OK'
        """
        # Mensal
        if (self.monthly_stop_loss > 0
                and self.monthly_pnl <= -self.monthly_stop_loss
                and not self.monthly_stopped):
            self.monthly_stopped = True
            log.warning(f"🚨 Stop MENSAL! PnL=${self.monthly_pnl:.2f}")
            return 'MONTHLY_STOP'
        # Semanal
        if (self.weekly_stop_loss > 0
                and self.weekly_pnl <= -self.weekly_stop_loss
                and not self.weekly_stopped):
            self.weekly_stopped = True
            log.warning(f"🛑 Stop SEMANAL! PnL=${self.weekly_pnl:.2f}")
            return 'WEEKLY_STOP'
        # Diario
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
        """True se o bot pode abrir novas posições (verifica todas as camadas)."""
        return not (self.daily_stopped or self.weekly_stopped or self.monthly_stopped)

    def journal_analysis(self) -> dict:
        """
        Análise do journal por tipo de entrada, sessão e regime.
        Retorna métricas para identificar quais condições geram alpha real.
        """
        import numpy as np
        if not self.trade_journal:
            return {"status": "sem trades no journal"}

        def stats(trades):
            if not trades: return {"count": 0, "wr": 0, "avg_pnl": 0, "pf": 0}
            pnls = [t["pnl"] for t in trades]
            wins = [p for p in pnls if p > 0]
            loss = [p for p in pnls if p < 0]
            return {
                "count":   len(trades),
                "wr":      round(len(wins)/len(trades)*100, 1),
                "avg_pnl": round(float(np.mean(pnls)), 4),
                "pf":      round(sum(wins)/max(abs(sum(loss)), 0.0001), 2),
            }

        # Agrupa por tipo
        by_type = {}
        for t in self.trade_journal:
            k = t["entry_type"] or "UNKNOWN"
            by_type.setdefault(k, []).append(t)

        # Agrupa por sessão
        by_session = {}
        for t in self.trade_journal:
            k = t["session"] or "UNKNOWN"
            by_session.setdefault(k, []).append(t)

        # Agrupa por regime
        by_regime = {}
        for t in self.trade_journal:
            k = t["regime"] or "UNKNOWN"
            by_regime.setdefault(k, []).append(t)

        return {
            "by_entry_type": {k: stats(v) for k, v in by_type.items()},
            "by_session":    {k: stats(v) for k, v in by_session.items()},
            "by_regime":     {k: stats(v) for k, v in by_regime.items()},
            "total_trades":  len(self.trade_journal),
            "overall":       stats(self.trade_journal),
        }

    def effective_score_threshold(self) -> int:
        """Score mínimo efetivo: mais alto após meta (modo conservador)."""
        return cfg.POST_TARGET_SCORE if self.daily_target_hit else cfg.MIN_ENTRY_SCORE

    def effective_risk_mult(self) -> float:
        """Multiplicador de risco: reduzido após meta diária."""
        return cfg.POST_TARGET_RISK / cfg.MAX_RISK_PCT if self.daily_target_hit else 1.0

    def to_dict(self) -> dict:
        stopped = self.daily_stopped or self.weekly_stopped or self.monthly_stopped
        mode = (
            "PARADO_MÊS"    if self.monthly_stopped else
            "PARADO_SEMANA" if self.weekly_stopped  else
            "PARADO_DIA"    if self.daily_stopped   else
            "CONSERVADOR"   if self.daily_target_hit else
            "ATIVO"
        )
        return {
            "daily_pnl":        round(self.daily_pnl, 4),
            "weekly_pnl":       round(self.weekly_pnl, 4),
            "monthly_pnl":      round(self.monthly_pnl, 4),
            "daily_target":     round(self.daily_target, 2),
            "daily_stop_loss":  round(self.daily_stop_loss, 2),
            "weekly_stop_loss": round(self.weekly_stop_loss, 2),
            "monthly_stop_loss":round(self.monthly_stop_loss, 2),
            "target_hit":       self.daily_target_hit,
            "stopped":          stopped,
            "mode":             mode,
            "can_trade":        self.can_trade(),
        }
