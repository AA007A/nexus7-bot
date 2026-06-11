"""
BGX Capital — Position Manager v1.0 (extraído de engine.py)
v12: Splitting do engine.py em submódulos para melhor manutenibilidade.

Responsabilidades:
  - Abertura de posições (_open)
  - Fechamento de posições (_close / close_all)
  - TPs parciais (check_partial_tps via risk.py)
  - Trailing stop (_apply_trailing_stops)
  - Verificação de R:R duplo (_check_rr_double)
  - Sincronização com Bybit (_sync_positions)
"""
from bot.logger import log
from bot.config import cfg

# Constante de taxa taker Bybit
TAKER_FEE = 0.00055


class PositionManagerMixin:
    """
    Mixin que adiciona métodos de gestão de posições ao TradingEngine.
    Importado em engine.py: class TradingEngine(PositionManagerMixin):

    Todos os métodos esperam que self tenha:
      - self.client: BybitClient
      - self.positions: dict
      - self.risk: RiskManager
      - self.stats: TradeStats
      - self.instruments: dict
      - self.paper_trade: bool
    """

    async def apply_trailing_stops(self):
        """
        Trailing Stop ATR-based progressivo.
        Ativa quando lucro >= TRAILING_TRIGGER do alvo.
        Lock distance = R × TRAILING_LOCK_R_MULT (default 1.0).
        Atualiza SL na exchange via set_sl() server-side.
        """
        for sym, pos in list(self.positions.items()):
            try:
                new_sl = pos.calc_trailing_sl()
                if new_sl is None:
                    continue
                better = (
                    new_sl > pos.trailing_sl if pos.direction == "LONG"
                    else new_sl < pos.trailing_sl
                )
                if better:
                    old_sl         = pos.trailing_sl
                    pos.trailing_sl = new_sl
                    await self.client.set_sl(
                        sym, new_sl, instruments=self.instruments
                    )
                    log.info(
                        f"🔄 Trailing SL {sym} {pos.direction}: "
                        f"{old_sl:.4f} → {new_sl:.4f}"
                    )
            except Exception as e:
                log.error(f"trailing_stops {sym}: {e}")

    async def check_partial_tps_all(self):
        """
        Executa TPs parciais 50%/50% para todas as posições abertas.
        Chama check_partial_tps do risk.py (conectado na v12).
        """
        from bot.risk import check_partial_tps, PositionRisk
        for sym in list(self.positions.keys()):
            pos = self.positions.get(sym)
            if not pos:
                continue
            cur = pos.current_price or pos.entry
            if cur <= 0:
                continue
            pr = self.risk.positions.get(sym)
            if pr and isinstance(pr, PositionRisk):
                try:
                    await check_partial_tps(pr, cur, self.client)
                except Exception as e:
                    log.error(f"partial_tps {sym}: {e}")

    async def emergency_close_all(self) -> dict:
        """
        Fecha TODAS as posições imediatamente.
        Endpoint: POST /api/close-all
        """
        if not self.positions:
            return {"closed": 0, "errors": 0, "symbols": []}

        closed, errors, symbols = 0, 0, []
        log.warning(f"🚨 EMERGENCY CLOSE ALL — {len(self.positions)} posições")

        from bot.notifier import notify
        from datetime import datetime

        for sym, pos in list(self.positions.items()):
            try:
                side      = "Sell" if pos.direction == "LONG" else "Buy"
                price     = pos.current_price or pos.entry
                pnl_gross = pos.pnl
                fee_open  = pos.qty * pos.entry * TAKER_FEE
                fee_close = pos.qty * price     * TAKER_FEE
                pnl_net   = pnl_gross - fee_open - fee_close

                if not self.paper_trade:
                    await self.client.place_order(
                        symbol=sym, side=side, qty=pos.qty,
                        sl=0, tp=0, instruments=self.instruments,
                    )

                del self.positions[sym]
                self.risk.close_position_risk(sym)
                symbols.append(sym)
                closed += 1
                log.info(f"✅ Emergency close {sym}: PnL net=${pnl_net:+.4f}")
                await notify(
                    f"🚨 *EMERGENCY CLOSE* `{sym}`\n"
                    f"PnL: `${pnl_net:+.4f}` | Dir: `{pos.direction}`"
                )
            except Exception as e:
                errors += 1
                log.error(f"Emergency close {sym}: {e}")

        return {"closed": closed, "errors": errors, "symbols": symbols}
