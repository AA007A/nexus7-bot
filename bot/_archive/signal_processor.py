"""
BGX Capital — Signal Processor v2.0
Mixin completo com lógica de scan e filtragem de sinais.

Responsabilidades:
  - Score threshold efetivo (normal vs pós-meta)
  - Filtro de símbolos viáveis por volume 24h
  - Ajuste de score por sessão de mercado
  - Verificação de regime (bloqueia direção proibida)
  - Filtro de correlação (via correlation.py)
  - Log de resumo de ciclo de scan
"""
from bot.logger import log
from bot.config import cfg


class SignalProcessorMixin:
    """
    Mixin que adiciona lógica de processamento de sinais ao TradingEngine.

    Requer que self tenha:
      - self.daily_tracker: DailyTracker
      - self.positions: dict
      - self.instruments: dict
      - self._get_market_session(): método (de engine.py)
      - self._REGIME_PARAMS: dict (de engine.py)
    """

    # ── Score e risco efetivos ─────────────────────────────────────
    def _effective_score(self) -> int:
        """Score mínimo atual: mais alto após meta diária."""
        return self.daily_tracker.effective_score_threshold()

    def _effective_risk_mult(self) -> float:
        """Multiplicador de risco atual: menor após meta diária."""
        return self.daily_tracker.effective_risk_mult()

    # ── Filtro de volume mínimo ────────────────────────────────────
    def _is_viable_symbol(self, symbol: str, volume_24h: float,
                           min_vol_usdt: float = 50_000_000) -> bool:
        """
        Verifica se símbolo tem volume suficiente para operar.
        Padrão: 50M USDT/24h mínimo para evitar slippage excessivo.
        """
        ok = volume_24h >= min_vol_usdt
        if not ok:
            log.debug(
                f"[{symbol}] Volume 24h ${volume_24h/1e6:.1f}M < "
                f"${min_vol_usdt/1e6:.0f}M mínimo → skip"
            )
        return ok

    # ── Ajuste de sessão de mercado ────────────────────────────────
    _SESSION_PENALTY: dict = {
        "ASIA":     {
            "SOLUSDT":   -8,
            "BNBUSDT":   -8,
            "XRPUSDT":   -5,
            "DOGEUSDT":  -10,
            "MATICUSDT": -8,
            "AVAXUSDT":  -8,
        },
        "LONDON":   {},
        "NEW_YORK": {},
    }

    def _session_score_adjustment(self, symbol: str, base_score: int) -> int:
        """Ajusta score com penalidade de sessão. Nunca vai abaixo de 0."""
        session = self._get_market_session()
        penalty = self._SESSION_PENALTY.get(session, {}).get(symbol, 0)
        if penalty:
            log.debug(
                f"[{symbol}] Sessão {session}: score {base_score}{penalty:+d} → {base_score + penalty}"
            )
        return max(0, base_score + penalty)

    # ── Filtro de regime ───────────────────────────────────────────
    def _regime_allows_direction(self, regime: str, direction: str) -> bool:
        """True se o regime permite abrir na direção do sinal."""
        rp      = self._REGIME_PARAMS.get(regime, self._REGIME_PARAMS["RANGING"])
        allowed = rp.get("allowed_sides", ["LONG", "SHORT"])
        if direction not in allowed:
            log.info(
                f"[Regime {regime}] Direção {direction} bloqueada "
                f"— permitido: {allowed}"
            )
            return False
        return True

    # ── Filtro de correlação ───────────────────────────────────────
    def _correlation_allows(self, symbol: str) -> bool:
        """
        Verifica correlação dinâmica com posições abertas via correlation.py.
        Fallback para grupos estáticos se correlation.py não disponível.
        """
        try:
            from bot.correlation import check_correlation
            result = check_correlation(symbol, self.positions)
            if not result["ok"]:
                log.info(f"[{symbol}] {result['reason']}")
                return False
            return True
        except ImportError:
            # Fallback: grupos estáticos hardcoded
            _CORR_GROUPS = [
                {"BTCUSDT", "ETHUSDT"},
                {"SOLUSDT", "AVAXUSDT", "DOTUSDT"},
                {"XRPUSDT", "ADAUSDT"},
                {"DOGEUSDT", "MATICUSDT"},
                {"LINKUSDT", "LTCUSDT"},
            ]
            for group in _CORR_GROUPS:
                if symbol in group:
                    for open_sym in self.positions:
                        if open_sym != symbol and open_sym in group:
                            log.info(
                                f"[{symbol}] Bloqueado: {open_sym} no mesmo grupo"
                            )
                            return False
            return True

    # ── Log de resumo ──────────────────────────────────────────────
    def _log_scan_summary(self, scanned: int, signals: int, opened: int):
        """Log resumido do ciclo de scan."""
        log.info(
            f"📊 Scan: {scanned} pares | {signals} sinais | "
            f"{opened} abertas | posições={len(self.positions)}/{cfg.MAX_POSITIONS}"
        )
