"""
BGX Capital Trading Engine v12.0
  ✅ Meta diária em % do saldo (escala com o capital)
  ✅ Stop-loss diário em % do saldo (proporcional ao capital)
  ✅ Modo agressivo até bater a meta → modo conservador depois
  ✅ Máximo 3 posições simultâneas (reduzido para controle de correlação)
  ✅ Score mínimo 60/100 para entrar (72 após meta diária)
  ✅ Trailing stop progressivo reativado — ATR-based
  ✅ TP parcial 50%/50% nos dois alvos técnicos
  ✅ SL/TP baseados em níveis técnicos reais (swing points)
  ✅ Controle de correlação entre pares (bloqueia corr > 0.70)
  ✅ filters.run_all_filters() conectado ao fluxo de execução real
  ✅ LEVERAGE=10, MAX_RISK_PCT=1% (era 50x/15% = 750% do saldo)
  ✅ Order Flow: CVD, OI, Funding, Delta Footprint
  ✅ Sentimento: Fear & Greed, News Pipeline
  ✅ CVD persistente com janela de 4h
  ✅ Sincronização em tempo real com Bybit
  ✅ Bug _update_balance corrigido (NameError silenciado)
  ✅ Bug pause/resume corrigido (_running flag)
  ✅ Endpoint /api/close-all disponível
"""
import asyncio, time
from datetime import datetime, timedelta
from typing import Dict, Optional, List

from bot.bybit import BybitClient, PAPER_TRADE
from bot.strategy import Analyzer, Signal
from bot.config import cfg
from bot.logger import log
from bot.notifier import (notify, signal_msg, order_opened_msg, close_msg,
    daily_report_msg, daily_target_msg, daily_stop_msg, drawdown_msg, consecutive_losses_msg, online_msg)
from bot import database as db
from bot import score as scoring
from bot import market_data as mdata
from bot import backtest as bt
from bot import filters as flts
from bot import correlation as corr_guard
from bot.daily_tracker import DailyTracker
from bot.position_manager import PositionManagerMixin
from bot.signal_processor import SignalProcessorMixin
from bot.risk import RiskManager, check_partial_tps
# v12: imports estáticos — eliminam __import__ dinâmicos do hot path (69k+ lookups/hora)
import numpy as _np
from datetime import datetime as _dt
from bot.indicators import ema as _ema_fn  # v11: RiskManager unificado (substitui classe local)


# ─── Trade (histórico fechado) ─────────────────────────────────────────────────
# Taxa Bybit: 0.055% por lado (maker) ou 0.055% taker — usamos 0.055% x2 = 0.11% total
TAKER_FEE = 0.00055   # 0.055% por execução

class Trade:
    def __init__(self, symbol, direction, entry, exit_price, qty, pnl_gross, opened_at,
                 fee_open=0.0, fee_close=0.0):
        self.symbol      = symbol
        self.direction   = direction
        self.entry       = entry
        self.exit_price  = exit_price
        self.qty         = qty
        self.opened_at   = opened_at
        self.closed_at   = datetime.utcnow()

        # Calcula taxas se não fornecidas explicitamente
        if fee_open == 0.0 and fee_close == 0.0:
            # fee = qty * preço * taxa_taker
            fee_open  = qty * entry      * TAKER_FEE
            fee_close = qty * exit_price * TAKER_FEE

        self.fee_open    = fee_open
        self.fee_close   = fee_close
        self.total_fees  = fee_open + fee_close
        self.pnl_gross   = pnl_gross              # PnL bruto (sem taxas)
        self.pnl         = pnl_gross - self.total_fees  # PnL LÍQUIDO (com taxas)


# ─── Position ──────────────────────────────────────────────────────────────────
class Position:
    def __init__(self, sig: Signal, qty: float):
        self.symbol      = sig.symbol
        self.direction   = sig.direction
        self.entry       = sig.entry
        self.sl          = sig.sl
        self.tp          = sig.tp
        self.score       = sig.score
        self.qty         = qty
        self.opened_at   = datetime.utcnow()
        self.pnl         = 0.0
        self.peak_pnl    = 0.0
        self.current_price = sig.entry
        # Trailing stop progressivo
        self.trailing_sl       = sig.sl
        self.trailing_active   = False
        self.trailing_milestone= 0
        # Tempo mínimo no trade: 3 candles de 15min = 45min
        self.min_hold_until    = datetime.utcnow().timestamp() + 90 * 60  # 90min = 6 candles 15M
        self.expected_pnl      = getattr(sig, 'expected_pnl', 0.0)
        self.total_fees_pct    = getattr(sig, 'total_fees', 0.0)
        # TP Parcial — dois alvos técnicos
        self.tp1               = getattr(sig, 'tp1', sig.tp)   # fecha 50% aqui
        self.tp2               = getattr(sig, 'tp2', sig.tp)   # fecha 50% aqui
        self.tp1_hit           = False    # já fechou metade no TP1?
        self.qty_original      = qty      # quantidade original para TP parcial
        self.rr1               = getattr(sig, 'rr1', sig.rr)
        self.rr2               = getattr(sig, 'rr2', sig.rr)

    def update_pnl(self, current_price: float):
        self.current_price = current_price
        if self.direction == "LONG":
            self.pnl = (current_price - self.entry) * self.qty
        else:
            self.pnl = (self.entry - current_price) * self.qty
        if self.pnl > self.peak_pnl:
            self.peak_pnl = self.pnl

    def pnl_pct(self) -> float:
        if self.entry <= 0 or self.qty <= 0:
            return 0.0
        if self.direction == "LONG":
            return (self.current_price - self.entry) / self.entry * 100 * cfg.LEVERAGE
        return (self.entry - self.current_price) / self.entry * 100 * cfg.LEVERAGE

    def calc_trailing_sl(self) -> Optional[float]:
        """
        Trailing Stop progressivo:
        - Ativa quando lucro >= 50% do alvo (TRAILING_TRIGGER)
        - Trava 25% abaixo do pico de lucro (TRAILING_LOCK)
        - Protege ganhos sem cortar o trade cedo demais
        """
        if self.pnl <= 0 or self.tp == self.entry:
            return None
        target = abs(self.tp - self.entry)
        if target <= 0:
            return None
        # Ativa trailing quando lucro >= TRAILING_TRIGGER % do alvo
        trigger_pnl = target * cfg.TRAILING_TRIGGER * self.qty
        if self.pnl < trigger_pnl:
            return None
        self.trailing_active = True
        # Trava TRAILING_LOCK % abaixo do pico de preço
        if self.direction == "LONG":
            peak_price = self.entry + (self.peak_pnl / self.qty if self.qty > 0 else 0)
            # v12: lock baseado em R × TRAILING_LOCK_R_MULT (era: peak_price × TRAILING_LOCK × 0.1)
            # R = distância entry→SL original; default = 1.0 × R abaixo do pico
            _r    = abs(self.entry - self.sl)
            _lock = _r * cfg.TRAILING_LOCK_R_MULT
            new_sl = peak_price - _lock
            return max(new_sl, self.sl)   # nunca recua abaixo do SL original
        else:
            peak_price = self.entry - (self.peak_pnl / self.qty if self.qty > 0 else 0)
            # v12: lock baseado em R × TRAILING_LOCK_R_MULT
            _r    = abs(self.entry - self.sl)
            _lock = _r * cfg.TRAILING_LOCK_R_MULT
            new_sl = peak_price + _lock
            return min(new_sl, self.sl)   # nunca recua acima do SL original

    def to_dict(self) -> dict:
        return {
            "symbol":           self.symbol,
            "direction":        self.direction,
            "entry":            round(self.entry, 6),
            "current_price":    round(self.current_price, 6),
            "sl":               round(self.trailing_sl, 6),
            "tp":               round(self.tp, 6),
            "qty":              self.qty,
            "pnl":              round(self.pnl, 4),
            "pnl_pct":          round(self.pnl_pct(), 2),
            "peak_pnl":         round(self.peak_pnl, 4),
            "trailing_active":  self.trailing_active,
            "trailing_sl":      round(self.trailing_sl, 6),
            "score":            self.score,
            "opened_at":        str(self.opened_at),
            "tp1":              round(self.tp1, 6),
            "tp2":              round(self.tp2, 6),
            "tp1_hit":          self.tp1_hit,
            "rr1":              self.rr1,
            "rr2":              self.rr2,
        }


# ─── Stats ────────────────────────────────────────────────────────────────────
class Stats:
    def __init__(self):
        self.trades: List[Trade] = []
        self.session_start = datetime.utcnow()

    def add(self, trade: Trade):
        self.trades.append(trade)

    def _filter(self, days: int = None) -> List[Trade]:
        if days is None:
            return self.trades
        cutoff = datetime.utcnow() - timedelta(days=days)
        return [t for t in self.trades if t.closed_at >= cutoff]

    def summary(self, days: int = None) -> dict:
        trades = self._filter(days)
        if not trades:
            return {
                "pnl": 0.0, "pnl_gross": 0.0, "total_fees": 0.0,
                "wins": 0, "losses": 0, "win_rate": 0.0, "trades": 0,
                "closed_trades": [],
            }
        wins   = [t for t in trades if t.pnl >= 0]   # pnl já é líquido
        losses = [t for t in trades if t.pnl < 0]
        return {
            "pnl":          round(sum(t.pnl       for t in trades), 4),  # LÍQUIDO
            "pnl_gross":    round(sum(t.pnl_gross for t in trades), 4),  # bruto
            "total_fees":   round(sum(t.total_fees for t in trades), 4), # total taxas
            "wins":         len(wins),
            "losses":       len(losses),
            "win_rate":     round(len(wins) / len(trades) * 100, 1),
            "trades":       len(trades),
            "closed_trades": [
                {
                    "symbol":    t.symbol,
                    "direction": t.direction,
                    "entry":     round(t.entry, 6),
                    "exit":      round(t.exit_price, 6),
                    "qty":       t.qty,
                    "pnl_gross": round(t.pnl_gross, 4),
                    "fees":      round(t.total_fees, 4),
                    "pnl":       round(t.pnl, 4),       # LÍQUIDO
                    "pnl_pct":   round(t.pnl / (t.entry * t.qty) * 100, 2) if t.entry * t.qty > 0 else 0,
                }
                for t in reversed(trades[-50:])
            ],
        }

    def all_summaries(self) -> dict:
        return {
            "session": self.summary(),
            "1d":      self.summary(1),
            "7d":      self.summary(7),
            "30d":     self.summary(30),
        }

    def daily_pnl(self) -> float:
        """PnL realizado apenas hoje (UTC)."""
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date()
        total = 0.0
        for t in self.trades:
            if t.closed_at.date() == today:
                total += t.pnl
        return total


# ──────────────────────────────────────────────────────────────────────────────
# DEPRECADO em v11/v12: RiskManager local substituído por bot.risk.RiskManager
#
# BUG CRÍTICO v12 CORRIGIDO: o bloco antigo abaixo tinha "class RiskManager_DEPRECATED:"
# como COMENTÁRIO, mas o corpo da classe (def __init__, def init, etc.) permaneceu
# com a MESMA indentação da classe Stats acima. O parser Python tratou esses métodos
# como pertencentes à classe Stats, sobrescrevendo Stats.__init__ — removendo
# "self.trades = []" do __init__ efetivo. Resultado: 'Stats' object has no
# attribute 'trades' em toda chamada self.stats.add(trade) / self.stats.daily_pnl().
#
# Bloco inteiro removido (RiskManager unificado já está em bot/risk.py).
# ──────────────────────────────────────────────────────────────────────────────


# ─── Trading Engine ───────────────────────────────────────────────────────────
class TradingEngine(PositionManagerMixin, SignalProcessorMixin):
    def __init__(self, client: BybitClient):
        self.client       = client
        self.analyzer     = Analyzer()
        self.risk         = RiskManager()
        self.stats        = Stats()
        self.positions:   Dict[str, Position] = {}
        self._trade_ids:  Dict[str, int] = {}   # symbol → DB trade id
        self.instruments: dict = {}
        self.viable_symbols: List[str] = []
        self.connected    = False
        self.active       = False
        self._running     = False
        self._scan_idx    = 0
        self._cooldown:   Dict[str, float] = {}   # símbolo → timestamp até quando não operar

        # ── Paper Trade ───────────────────────────────────────────
        self.paper_trade = PAPER_TRADE

        # ── Daily Tracker (submódulo extraído do engine) ──────────
        self.daily_tracker = DailyTracker()
        if self.paper_trade:
            log.warning(
                "🟡 PAPER TRADE MODE — posições NÃO afetam capital real. "
                "Risk manager opera em modo simulado."
            )

        # ── Meta diária (calculada em % do saldo real) ───────────
        self.daily_pnl        = 0.0
        self.daily_target     = cfg.DAILY_TARGET      # fallback USD
        self.daily_stop_loss  = cfg.DAILY_STOP_LOSS   # fallback USD
        self.daily_target_hit = False
        self.daily_stopped    = False
        self._last_reset_day  = -1

    def _recalc_daily_limits(self):
        """Recalcula meta/stop diários em % do saldo (escala com capital)."""
        bal = self.risk.balance
        if bal > 0:
            self.daily_target    = bal * cfg.DAILY_TARGET_PCT
            self.daily_stop_loss = bal * cfg.DAILY_STOP_LOSS_PCT

    # ── Lifecycle ──────────────────────────────────────────────
    async def run(self):
        if self._running:
            return
        self._running = True
        log.info("⚡ Engine v11 iniciando...")
        await db.init()   # inicia DB (PostgreSQL ou SQLite)
        asyncio.create_task(scoring.update_macro_cache())          # scoring cache
        asyncio.create_task(scoring.news_reader_loop())             # news 24/7
        asyncio.create_task(mdata.update_macro_correlations())      # DXY/S&P
        asyncio.create_task(bt.weekly_backtest_loop(self.client))   # backtest semanal
        asyncio.create_task(flts.update_fear_greed())               # Fear&Greed (v11: conectado)
        asyncio.create_task(flts.update_macro_events())             # Macro events USD (v11: conectado)
        await self._connect()

        while self._running:
            try:
                if not self.connected:
                    await asyncio.sleep(20)   # scan a cada 20s
                    await self._connect()
                    continue

                if self.active:
                    self._check_daily_reset()
                    await self._update_balance()
                    await self._sync_positions()
                    await self._apply_trailing_stops()
                    # v12: TPs parciais 50%/50% via risk.py (era ignorado)
                    # check_partial_tps: TP1 fecha 50% e move SL para BE
                    #                    TP2 fecha os 50% restantes
                    #                    trailing stop entre TP1 e TP2
                    for _sym, _pos in list(self.positions.items()):
                        _cur = _pos.current_price or _pos.entry
                        if _cur > 0 and hasattr(_pos, "tp1"):
                            from bot.risk import PositionRisk as _PR
                            _pr = self.risk.positions.get(_sym)
                            if _pr and isinstance(_pr, _PR):
                                await check_partial_tps(_pr, _cur, self.client)
                    await self._check_rr_double()
                    self._update_daily_pnl()
                    
                    if self.daily_stopped:
                        log.warning("🛑 Stop-loss diário ativado")
                    elif self.risk.can_open(len(self.positions)):
                        await self._scan_all_and_enter()

                await asyncio.sleep(5)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Engine loop: {e}")
                await asyncio.sleep(5)

    def stop(self):
        """
        Pausa o bot: para entradas novas mas mantém posições abertas protegidas.
        BUG CORRIGIDO: _running=False garante que run() pode ser reiniciado.
        """
        self.active   = False
        self._running = False   # era: apenas active=False → run() ficava preso
        log.info("⏸️ Bot pausado (servidor continua | posições mantidas com SL/TP na exchange)")

    async def close_all_positions(self) -> dict:
        """
        Fecha TODAS as posições abertas imediatamente.
        Endpoint de emergência: POST /api/close-all
        """
        if not self.positions:
            return {"closed": 0, "errors": 0, "symbols": []}

        closed, errors, symbols = 0, 0, []
        log.warning(f"🚨 EMERGENCY CLOSE ALL — {len(self.positions)} posições")

        for sym, pos in list(self.positions.items()):
            try:
                side = "Sell" if pos.direction == "LONG" else "Buy"
                await self.client.place_order(
                    symbol=sym, side=side, qty=pos.qty, sl=0, tp=0,
                )
                pnl_gross = pos.pnl
                fee_open  = pos.qty * pos.entry * TAKER_FEE
                fee_close = pos.qty * pos.current_price * TAKER_FEE
                pnl_net   = pnl_gross - fee_open - fee_close
                trade = Trade(
                    sym, pos.direction, pos.entry, pos.current_price,
                    pos.qty, pnl_gross, pos.opened_at,
                    fee_open=fee_open, fee_close=fee_close,
                )
                self.stats.add(trade)
                del self.positions[sym]
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

    # ── Meta diária ────────────────────────────────────────────
    def _check_daily_reset(self):
        """Reseta contadores de PnL diário à meia-noite UTC."""
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).day
        if today != self._last_reset_day:
            if self._last_reset_day != -1:
                log.info(
                    f"📅 Novo dia UTC — resetando contadores. "
                    f"PnL ontem: ${self.daily_pnl:+.4f} | "
                    f"Meta {'✅ BATIDA' if self.daily_target_hit else '❌ não atingida'}"
                )
            self.daily_pnl        = 0.0
            self.daily_target_hit = False
            self.daily_stopped    = False
            self._last_reset_day  = today
            # Volta ao score padrão após reset
            self._recalc_daily_limits()
            log.info(f"🎯 Meta diária: ${self.daily_target:.2f} ({cfg.DAILY_TARGET_PCT*100:.1f}% saldo) | Stop: -${self.daily_stop_loss:.2f}")

    def _update_daily_pnl(self):
        """Soma PnL realizado + não-realizado do dia e verifica meta/stop."""
        realized    = self.stats.daily_pnl()
        unrealized  = sum(p.pnl for p in self.positions.values())
        self.daily_pnl = realized + unrealized

        # ── Stop-loss diário ──────────────────────────────────────
        if not self.daily_stopped and self.daily_pnl <= -self.daily_stop_loss:
            self.daily_stopped = True
            log.warning(
                f"🛑 STOP-LOSS DIÁRIO ATINGIDO: ${self.daily_pnl:.4f} ≤ -${self.daily_stop_loss:.0f}"
                f" — Bot pausado até meia-noite UTC"
            )
            asyncio.create_task(notify(
                f"🛑 *STOP-LOSS DIÁRIO ATINGIDO*\n"
                f"Perda: `${self.daily_pnl:.2f}` | Limite: `-${self.daily_stop_loss:.0f}`\n"
                f"Bot pausado até meia-noite UTC"
            ))

        # ── Meta batida ───────────────────────────────────────────
        if not self.daily_target_hit and self.daily_pnl >= self.daily_target:
            self.daily_target_hit = True
            log.info(
                f"🎯 META DIÁRIA BATIDA! PnL=${self.daily_pnl:.4f} ≥ ${self.daily_target:.0f}"
                f" — Entrando em modo CONSERVADOR (score ≥ {cfg.POST_TARGET_SCORE})"
            )
            asyncio.create_task(notify(
                f"🎯 *META DIÁRIA BATIDA!*\n"
                f"Lucro: `+${self.daily_pnl:.2f} USDT`\n"
                f"Próximas entradas: score >= `{cfg.POST_TARGET_SCORE}/100`\n"
                f"Modo conservador ativado"
            ))

    def _effective_score(self) -> int:
        """Score mínimo efetivo — aumenta após bater a meta."""
        if self.daily_target_hit:
            return cfg.POST_TARGET_SCORE  # mais seletivo (88)
        return cfg.MIN_ENTRY_SCORE        # padrão (60)

    def _effective_risk_pct(self) -> float:
        """Risco por trade — reduz após bater a meta."""
        if self.daily_target_hit:
            return cfg.POST_TARGET_RISK   # conservador (15%)
        return cfg.MAX_RISK_PCT           # padrão (30%)

    # ── Connect ────────────────────────────────────────────────
    async def _connect(self):
        try:
            # Ping é opcional — não bloqueia o bot se falhar
            # O bot tenta operar mesmo sem ping (REST pode funcionar)
            ping_ok = await self.client.ping()
            if not ping_ok:
                log.warning("⚠️ Bybit ping falhou — continuando mesmo assim (REST pode funcionar)")

            bal = await self.client.get_balance()
            if bal < 0:
                log.error("❌ Autenticação falhou")
                self.connected = False
                return

            self.risk.init(bal)
            self.risk.update(bal)
            self.instruments = await self.client.get_instruments()
            await self._filter_viable_symbols()

            for sym in self.viable_symbols[:15]:
                await self.client.set_leverage(sym, cfg.LEVERAGE)
                await asyncio.sleep(0.3)

            await self._load_existing_positions()
            self.connected = True
            self.active    = True

            # Inicia WebSocket para dados em tempo real
            # Garante que viable_symbols foi populado antes de iniciar o WS
            ws_symbols = self.viable_symbols[:10]
            if not ws_symbols:
                log.warning(
                    "⚠️ viable_symbols vazio — usando fallback cfg.SYMBOLS[:10] para WebSocket"
                )
                ws_symbols = cfg.SYMBOLS[:10]

            log.info(
                f"🔌 Iniciando WebSocket com {len(ws_symbols)} símbolos: "
                f"{', '.join(ws_symbols)}"
            )
            await self.client.start_websocket(
                ws_symbols,
                intervals=["15", "60", "240"],
            )
            log.info(f"✅ Conectado! ${bal:.4f} USDT | {len(self.viable_symbols)} pares | max {cfg.MAX_POSITIONS} posições | score >= {cfg.MIN_ENTRY_SCORE}")

            await notify(await online_msg(bal, bal*cfg.LEVERAGE, len(self.viable_symbols), cfg.MAX_POSITIONS))
            await notify(
                f"Score mínimo: `{cfg.MIN_ENTRY_SCORE}/100`\n"
                f"Pares ativos: `{len(self.viable_symbols)}`"
            )
        except Exception as e:
            log.error(f"_connect: {e}")
            self.connected = False

    async def _filter_viable_symbols(self):
        try:
            tickers    = await self.client.get_all_tickers()
            price_map  = {t["symbol"]: float(t.get("lastPrice", 0)) for t in tickers}
            buying_power = self.risk.balance * cfg.LEVERAGE
            viable = []
            for sym in cfg.SYMBOLS:
                info  = self.instruments.get(sym)
                price = price_map.get(sym, 0)
                if not info or price <= 0:
                    continue
                min_cost = max(info.get("minNotional", 1.0), info.get("minQty", 0.001) * price)
                if buying_power >= min_cost * 1.1:
                    viable.append(sym)
            self.viable_symbols = viable
            log.info(f"✅ {len(viable)} pares viáveis")
        except Exception as e:
            log.error(f"_filter_viable: {e}")
            self.viable_symbols = cfg.SYMBOLS[:5]

    # ── Sincronização em tempo real com Bybit ──────────────────
    async def _sync_positions(self):
        """Puxa posições abertas do Bybit e reconcilia estado local."""
        try:
            all_pos   = await self.client.get_positions()
            open_syms = {}
            for p in all_pos:
                if float(p.get("size", 0)) > 0:
                    open_syms[p["symbol"]] = p

            # Posições fechadas remotamente
            for sym in list(self.positions.keys()):
                pos = self.positions[sym]
                if sym not in open_syms:
                    # Respeita tempo mínimo no trade (só SL/TP da Bybit fecha)
                    # Se a Bybit fechou, aceita — mas loga o motivo
                    hold_left = pos.min_hold_until - time.time()
                    if hold_left > 0:
                        log.warning(
                            f"⚠️ {sym} fechado pela Bybit antes do tempo mínimo "
                            f"({hold_left/60:.0f}min restantes) — SL atingido"
                        )
                    # PnL bruto (sem taxas)
                    pnl_gross = pos.pnl
                    exit_px   = pos.current_price or pos.entry

                    # Taxas: 0.055% por lado (taker Bybit)
                    fee_open  = pos.qty * pos.entry * TAKER_FEE
                    fee_close = pos.qty * exit_px   * TAKER_FEE
                    total_fee = fee_open + fee_close
                    pnl_net   = pnl_gross - total_fee   # PnL LÍQUIDO

                    trade = Trade(
                        sym, pos.direction, pos.entry, exit_px,
                        pos.qty, pnl_gross, pos.opened_at,
                        fee_open=fee_open, fee_close=fee_close
                    )
                    self.stats.add(trade)
                    # Persiste fechamento no banco
                    tid = self._trade_ids.pop(sym, 0)
                    if tid:
                        await db.save_trade_close(
                            tid, price, pnl_net, total_fee,
                            (datetime.utcnow() - pos.opened_at).total_seconds() / 60
                        )
                    del self.positions[sym]
                    self._cooldown[sym] = time.time() + 1800

                    # ── 3 perdas consecutivas: registra, bot CONTINUA ──
                    consecutive = await db.update_consecutive_losses(pnl_net)
                    if consecutive >= 3:
                        log.warning(
                            f"⚠️ {consecutive} perdas consecutivas — registrado, bot continua"
                        )
                        _cbal = await self.client.get_balance()
                        await notify(await consecutive_losses_msg(consecutive, _cbal, _cbal*cfg.LEVERAGE))
                        await db.save_risk_event(
                            "CONSECUTIVE_LOSSES",
                            f"{consecutive} perdas consecutivas",
                            pnl_net,
                        )
                    icon = "✅" if pnl_net >= 0 else "❌"
                    log.info(
                        f"📭 {sym} fechado | Bruto=${pnl_gross:+.4f} "
                        f"Taxas=-${total_fee:.4f} | Líquido=${pnl_net:+.4f}"
                    )
                    _bal = await self.client.get_balance()
                    await notify(await close_msg(sym, pos.direction, pnl_net, pos.pnl_pct(), exit_px, _bal, _bal*cfg.LEVERAGE))
                else:
                    # Atualiza dados da posição aberta
                    bp = open_syms[sym]
                    cur = float(bp.get("markPrice", pos.current_price))
                    upnl = float(bp.get("unrealisedPnl", pos.pnl))
                    pos.update_pnl(cur)
                    pos.pnl = upnl

            # Posições abertas externamente (ex: manual)
            for sym, bp in open_syms.items():
                if sym not in self.positions:
                    ep  = float(bp.get("avgPrice", 0))
                    sz  = float(bp.get("size", 0))
                    side = bp.get("side", "Buy")
                    if ep > 0 and sz > 0:
                        direction = "LONG" if side == "Buy" else "SHORT"
                        atr_est = ep * 0.007
                        if direction == "LONG":
                            sl = ep - atr_est * 1.5
                            tp = ep + atr_est * 3.0
                        else:
                            sl = ep + atr_est * 1.5
                            tp = ep - atr_est * 3.0
                        sig = Signal(sym, direction, ep, sl, tp, 0.75, "sync Bybit", 75)
                        pos = Position(sig, sz)
                        pos.pnl = float(bp.get("unrealisedPnl", 0))
                        cur = float(bp.get("markPrice", ep))
                        pos.update_pnl(cur)
                        self.positions[sym] = pos
                        log.info(f"📥 Posição externa carregada: {sym} {direction}")

        except Exception as e:
            log.error(f"_sync_positions: {e}")

    # ── Trailing stop DESATIVADO ────────────────────────────────


    async def _monitor_news_pipeline(self):
        """
        Monitora o pipeline e alerta quando notícia de alto impacto aparece.
        Rodado a cada ciclo de background (30min).
        """
        try:
            from bot.news_pipeline import _pipeline_cache, get_pipeline_status
            from bot.notifier import high_impact_news_msg, news_summary_msg

            if not _pipeline_cache:
                return

            # Alertar top notícia se relevância >= 80 e recente (<30min)
            import time as _t
            for item in _pipeline_cache[:5]:
                if (item.relevance >= 80 and
                    item.sentiment != "NEUTRAL" and
                    (_t.time() - item.timestamp) < 1800):
                    from bot.news_pipeline import get_news_impact
                    impact = get_news_impact("LONG")
                    await notify(await high_impact_news_msg(
                        item.title, item.source, item.sentiment,
                        item.relevance, impact.get("score_pts", 0)
                    ))
                    break   # apenas 1 alerta por ciclo

            # Resumo a cada 6h
            now_h = _dt.utcnow().hour  # v12: static import
            if now_h in (0, 6, 12, 18):
                st = get_pipeline_status()
                if st["total"] > 0:
                    top = [
                        {"source": i.source, "sentiment": i.sentiment,
                         "title": i.title}
                        for i in _pipeline_cache[:3]
                    ]
                    await notify(await news_summary_msg(
                        st["total"], st["bullish"], st["bearish"],
                        st["sources"], top
                    ))
        except Exception as e:
            log.debug(f"_monitor_news_pipeline: {e}")

    async def _notify_session_info(self):
        """Envia info da sessão atual no Telegram uma vez por hora."""
        sess = get_market_session()
        if sess["quality"] < 50:
            await notify(
                f"{sess['emoji']} *SESSÃO FRACA — Bot em modo cauteloso*\n"
                f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
                f"⏰ Sessão: `{sess['session']}`\n"
                f"📊 Qualidade: `{sess['quality']}%`\n"
                f"📋 `{sess['description']}`\n"
                f"_Aguardando sessão de maior liquidez..._"
            )
        else:
            log.info(f"{sess['emoji']} Sessão: {sess['session']} (q={sess['quality']}%)")

    async def _apply_trailing_stops(self):
        """
        Trailing Stop REATIVADO — ATR-based progressivo.
        Ativa quando lucro >= TRAILING_TRIGGER (50%) do alvo.
        Trava TRAILING_LOCK (25%) abaixo do pico de preço.
        SL nunca recua além do SL técnico original.
        Atualiza SL na exchange via set_sl() (server-side).
        """
        for sym, pos in list(self.positions.items()):
            try:
                new_sl = pos.calc_trailing_sl()
                if new_sl is None:
                    continue

                # Verifica se o novo SL é melhor que o atual
                if pos.direction == "LONG":
                    better = new_sl > pos.trailing_sl
                else:
                    better = new_sl < pos.trailing_sl

                if better:
                    old_sl = pos.trailing_sl
                    pos.trailing_sl = new_sl
                    await self.client.set_sl(sym, new_sl)
                    log.info(
                        f"🔄 Trailing SL {sym} {pos.direction}: "
                        f"{old_sl:.4f} → {new_sl:.4f} "
                        f"(pico PnL=${pos.peak_pnl:.4f})"
                    )
            except Exception as e:
                log.error(f"_apply_trailing_stops {sym}: {e}")

    # ── Fecha posição quando lucro = 2x o risco (R:R dobrado) ──
    def _effective_risk_pct(self) -> float:
        """Retorna o risco efetivo, reduzido se a meta diária foi batida."""
        # BUG CORRIGIDO v12.1: daily_pnl é MÉTODO de Stats, faltava "()"
        # Comparar método (bound method) com número sempre seria TypeError/ambiguo.
        # Usa self.daily_tracker (já calcula meta corretamente) em vez de duplicar lógica.
        if self.daily_tracker.daily_target_hit:
            return cfg.POST_TARGET_RISK
        return cfg.MAX_RISK_PCT

    async def _check_rr_double(self):
        """
        Fecha a posição quando o lucro atingir o dobro do risco original.
        Ex: risco = $5 → fecha quando lucro = $10.
        NÃO fecha por ruído, micro reversões ou trailing.
        Só fecha 100% da posição — sem parciais.
        """
        for sym, pos in list(self.positions.items()):
            try:
                risk_dist   = abs(pos.entry - pos.sl)   # distância SL original
                if risk_dist <= 0:
                    continue

                price = pos.current_price or pos.entry
                if pos.direction == "LONG":
                    lucro_dist = price - pos.entry
                else:
                    lucro_dist = pos.entry - price

                # Lucro atingiu o dobro do risco? → fecha 100%
                if lucro_dist >= risk_dist * 2.0:
                    rr_atual = lucro_dist / risk_dist
                    log.info(
                        f"🎯 {sym} R:R dobrado! "
                        f"Lucro={lucro_dist:.4f} ≥ 2x Risco={risk_dist:.4f} "
                        f"(R:R={rr_atual:.2f}) → fechando 100%"
                    )
                    close_side = "Sell" if pos.direction == "LONG" else "Buy"
                    await self.client.place_order(
                        symbol=sym, side=close_side,
                        qty=pos.qty, sl=0, tp=0,
                    )
                    # Registra como trade fechado
                    pnl_gross = lucro_dist * pos.qty
                    fee_open  = pos.qty * pos.entry * TAKER_FEE
                    fee_close = pos.qty * price     * TAKER_FEE
                    total_fee = fee_open + fee_close
                    pnl_net   = pnl_gross - total_fee
                    # BUG CORRIGIDO: taxa não é mais recalculada (código morto removido)
                    trade = Trade(
                        sym, pos.direction, pos.entry, price,
                        pos.qty, pnl_gross, pos.opened_at,
                        fee_open=fee_open, fee_close=fee_close,
                    )
                    self.stats.add(trade)
                    # Persiste fechamento no banco
                    tid = self._trade_ids.pop(sym, 0)
                    if tid:
                        await db.save_trade_close(
                            tid, price, pnl_net, total_fee,
                            (datetime.utcnow() - pos.opened_at).total_seconds() / 60
                        )
                    del self.positions[sym]
                    self._cooldown[sym] = time.time() + 1800

                    # ── 3 perdas consecutivas: registra, bot CONTINUA ──
                    consecutive = await db.update_consecutive_losses(pnl_net)
                    if consecutive >= 3:
                        log.warning(
                            f"⚠️ {consecutive} perdas consecutivas — registrado, bot continua"
                        )
                        _cbal = await self.client.get_balance()
                        await notify(await consecutive_losses_msg(consecutive, _cbal, _cbal*cfg.LEVERAGE))
                        await db.save_risk_event(
                            "CONSECUTIVE_LOSSES",
                            f"{consecutive} perdas consecutivas",
                            pnl_net,
                        )
                    _bal2 = await self.client.get_balance()
                    await notify(await close_msg(sym, pos.direction, pnl_net, pos.pnl_pct(), price, _bal2, _bal2*cfg.LEVERAGE))
            except Exception as e:
                log.error(f"_check_rr_double {sym}: {e}")

    # ── Scan & Enter ────────────────────────────────────────────
    async def _scan_all_and_enter(self):
        """
        Multi-Timeframe scan: busca 15m, 1h e 4h para cada símbolo.
        Fast-track: usa WebSocket cache quando disponível (sem latência REST).
        Fallback: busca os três timeframes em paralelo via asyncio.gather().
        Só entra quando AMBOS os timeframes apontam na mesma direção.
        """
        candidates = []
        min_score = self._effective_score()

        # Thresholds mínimos para considerar o cache WS "suficiente"
        WS_MIN_15  = 20
        WS_MIN_1H  = 15
        WS_MIN_4H  = 10
        # Thresholds mínimos para prosseguir com a análise (após REST fallback)
        ANAL_MIN_15 = 20
        ANAL_MIN_1H = 15
        ANAL_MIN_4H = 10

        for sym in self.viable_symbols:
            if sym in self.positions:
                continue
            cooldown_left = self._cooldown.get(sym, 0) - time.time()
            if cooldown_left > 0:
                log.debug(f"[{sym}] cooldown {cooldown_left/60:.0f}min → skip")
                continue
            try:
                # ── Fast-track: WebSocket cache (zero latência REST) ──
                k15 = self.client.get_cached_klines(sym, "15",  100)
                k1h = self.client.get_cached_klines(sym, "60",  100)
                k4h = self.client.get_cached_klines(sym, "240", 100)

                ws_hit = (
                    len(k15) >= WS_MIN_15
                    and len(k1h) >= WS_MIN_1H
                    and len(k4h) >= WS_MIN_4H
                )

                if ws_hit:
                    log.info(
                        f"🔍 [{sym}] WS cache hit "
                        f"(15m={len(k15)} 1h={len(k1h)} 4h={len(k4h)}) — sem REST"
                    )
                else:
                    # ── Fallback REST paralelo (sem delays sequenciais) ──
                    missing = []
                    if len(k15) < WS_MIN_15:
                        missing.append(("15",  100))
                    if len(k1h) < WS_MIN_1H:
                        missing.append(("60",  100))
                    if len(k4h) < WS_MIN_4H:
                        missing.append(("240", 100))

                    log.info(
                        f"🔍 [{sym}] WS cache miss "
                        f"(15m={len(k15)} 1h={len(k1h)} 4h={len(k4h)}) "
                        f"— REST paralelo para {[m[0] for m in missing]}"
                    )

                    results = await asyncio.gather(
                        *[self.client.get_klines(sym, iv, lim) for iv, lim in missing],
                        return_exceptions=True,
                    )

                    idx = 0
                    if len(k15) < WS_MIN_15:
                        r = results[idx]; idx += 1
                        if not isinstance(r, Exception):
                            k15 = r
                    if len(k1h) < WS_MIN_1H:
                        r = results[idx]; idx += 1
                        if not isinstance(r, Exception):
                            k1h = r
                    if len(k4h) < WS_MIN_4H:
                        r = results[idx]; idx += 1
                        if not isinstance(r, Exception):
                            k4h = r

                if len(k15) < ANAL_MIN_15 or len(k1h) < ANAL_MIN_1H or len(k4h) < ANAL_MIN_4H:
                    log.debug(
                        f"[{sym}] dados insuficientes após fetch "
                        f"(15m={len(k15)} 1h={len(k1h)} 4h={len(k4h)}) → skip"
                    )
                    continue

                sig = self.analyzer.analyze_mtf(
                    sym, k15, k1h, k4h,
                    min_score=min_score,
                    fee_mult=cfg.FEE_MULTIPLIER,
                    vol_mult=cfg.MIN_VOLUME_MULT,
                )
                # Alimenta cache de correlação com closes recentes
                if k15:
                    closes_15 = [k["c"] for k in k15[-50:]]
                    corr_guard.seed_closes(sym, closes_15)

                if sig:
                    if sig.expected_pnl <= 0:
                        log.info(f"[{sym}] PnL negativo após taxas → HOLD")
                        continue
                    candidates.append(sig)
                    log.info(
                        f"🎯 SINAL PREMIUM: {sym} score={sig.score}/100 "
                        f"{sig.direction} R:R={sig.rr} "
                        f"PnL_líq≈+{sig.expected_pnl:.2f}% | {sig.reason}"
                    )
                    signal_reason = (
                        f"{sig.reason} | regime={getattr(sig,'regime','TREND')} "
                        f"RSI={getattr(sig,'rsi',0):.0f} vol={getattr(sig,'vol_ratio',0):.2f}x "
                        f"4H=↑ 1H=↑"
                    )
                    await db.log_decision(sym, "SIGNAL", sig.score, signal_reason)
                else:
                    # Mostra score parcial para diagnóstico
                    try:
                        from bot.strategy import score_tf, detect_regime
                        from bot.indicators import atr as atr_fn
                        import numpy as np
                        def ga(kl): return ([k["c"] for k in kl],[k["h"] for k in kl],[k["l"] for k in kl],[k["o"] for k in kl],[k["v"] for k in kl])
                        c15,h15,l15,o15,v15 = ga(k15)
                        c1h,h1h,l1h,o1h,v1h = ga(k1h)
                        c4h,h4h,l4h,o4h,v4h = ga(k4h)
                        def get_atr(h,l,c):
                            a=atr_fn(h,l,c); return a[-1], float(np.mean(a[-20:])) if len(a)>=20 else a[-1]
                        av15,ag15=get_atr(h15,l15,c15)
                        av1h,ag1h=get_atr(h1h,l1h,c1h)
                        av4h,ag4h=get_atr(h4h,l4h,c4h)
                        # v12: imports estáticos — removidos __import__ dinâmicos do hot path
                        e20_4h = _ema_fn(c4h, 20)[-1]
                        e50_4h = _ema_fn(c4h, 50)[-1]
                        e20_1h = _ema_fn(c1h, 20)[-1]
                        e50_1h = _ema_fn(c1h, 50)[-1]
                        bull_4h = not _np.isnan(e20_4h) and e20_4h>e50_4h and c4h[-1]>e20_4h
                        bear_4h = not _np.isnan(e20_4h) and e20_4h<e50_4h and c4h[-1]<e20_4h
                        bull_1h = not _np.isnan(e20_1h) and e20_1h>e50_1h and c1h[-1]>e20_1h
                        bear_1h = not _np.isnan(e20_1h) and e20_1h<e50_1h and c1h[-1]<e20_1h
                        direction = "LONG" if (bull_4h or bull_1h) else "SHORT"
                        s4=score_tf(c4h,h4h,l4h,o4h,v4h,direction,av4h,ag4h)
                        s1=score_tf(c1h,h1h,l1h,o1h,v1h,direction,av1h,ag1h)
                        s15=score_tf(c15,h15,l15,o15,v15,direction,av15,ag15)
                        combined=round(s4["total"]*0.30+s1["total"]*0.30+s15["total"]*0.40)
                        regime=detect_regime(c4h,h4h,l4h,av4h)
                        from bot.indicators import rsi as rsi_fn
                        rsi_v=rsi_fn(c15)[-1]
                        vols=_np.array(v15); avg_vol=vols[-21:-1].mean() if len(vols)>21 else vols.mean() or 1  # v12: static import
                        vol_r=vols[-1]/avg_vol
                        log.info(
                            f"[{sym}] Score={combined}/100 (4H:{s4['total']} 1H:{s1['total']} 15M:{s15['total']}) "
                            f"| regime={regime} RSI={rsi_v:.0f} vol={vol_r:.2f}x "
                            f"| 4H={'↑' if bull_4h else '↓' if bear_4h else '→'} "
                            f"1H={'↑' if bull_1h else '↓' if bear_1h else '→'} → HOLD"
                        )
                    except Exception as ex:
                        log.info(f"[{sym}] ✗ Sem sinal")
                        combined = 0
                        regime = "UNKNOWN"
                        rsi_v = 0
                        vol_r = 0
                    # Salva score real no banco para o dashboard
                    hold_reason = (
                        f"regime={regime} RSI={rsi_v:.0f} vol={vol_r:.2f}x "
                        f"4H={'↑' if locals().get('bull_4h') else '↓' if locals().get('bear_4h') else '→'} "
                        f"1H={'↑' if locals().get('bull_1h') else '↓' if locals().get('bear_1h') else '→'}"
                    )
                    await db.log_decision(sym, "HOLD", combined, hold_reason)
                    # Alerta "quase entrando" — score entre 55 e cfg.MIN_ENTRY_SCORE-1
                    if cfg.MIN_ENTRY_SCORE - 5 <= combined < cfg.MIN_ENTRY_SCORE:
                        asyncio.create_task(notify(
                            f"🔔 *QUASE ENTRANDO — {sym}*\n"
                            f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
                            f"📊 Score: `{combined}/{cfg.MIN_ENTRY_SCORE}` (faltam {cfg.MIN_ENTRY_SCORE - combined}pts)\n"
                            f"📍 Par: `{sym}`\n"
                            f"🕐 Regime: `{regime}`\n"
                            f"_Monitorando..._"
                        ))
            except Exception as e:
                log.error(f"scan {sym}: {e}")

        # Ordena por score decrescente e entra nos melhores
        candidates = self.analyzer.rank_signals(candidates)
        for sig in candidates:
            if len(self.positions) >= cfg.MAX_POSITIONS:
                break
            await self._open(sig)

    async def _open(self, sig: Signal):
        try:
            # ── 0. Filtros de proteção (CONECTADOS ao fluxo real) ────
            # BUG CORRIGIDO: filters.run_all_filters() agora é chamado aqui
            filter_result = await flts.run_all_filters(
                self.client, sig.symbol, sig.direction
            )
            if not filter_result["ok"]:
                log.info(
                    f"[{sig.symbol}] 🚫 Filtro '{filter_result['blocked_by']}' "
                    f"bloqueou entrada: {filter_result['details'].get(filter_result['blocked_by'], {}).get('reason', '')}"
                )
                return

            size_mult = filter_result.get("size_mult", 1.0)

            # ── 1. Controle de correlação ────────────────────────────
            corr_result = corr_guard.check_correlation(sig.symbol, self.positions)
            if not corr_result["ok"]:
                log.info(f"[{sig.symbol}] 🔗 {corr_result['reason']}")
                return

            # ── 2. Atualizar saldo real antes de calcular qty ────────
            fresh_bal = await self.client.get_balance()
            if fresh_bal > 0:
                self.risk.update(fresh_bal)
                self._recalc_daily_limits()
            # Combina size_mult do filtro (horário/weekend) com size_mult do regime (volatilidade)
            # O sinal já foi aprovado pelo strategy.analyze_mtf que inclui regime.classify()
            regime_mult = getattr(sig, "regime_size_mult", 1.0)
            combined_mult = size_mult * regime_mult
            qty = self.risk.size(sig.symbol, sig.entry, self.instruments, combined_mult)
            if qty <= 0:
                log.warning(f"⚠️ {sig.symbol}: qty=0 — saldo insuficiente (${self.risk.balance:.2f})")
                return

            # ── Score pré-trade ───────────────────────────────
            # Buscar klines para pré-trade — REST se cache insuficiente
            kl = self.client.get_cached_klines(sig.symbol, "15", 50)
            if len(kl) < 20:
                try:
                    kl = await self.client.get_klines(sig.symbol, "15", 50)
                except Exception:
                    kl = []

            if len(kl) >= 10:
                c = [float(k.get("c", sig.entry) if isinstance(k, dict) else (k[4] if len(k) > 4 else sig.entry)) for k in kl]
                h = [float(k.get("h", sig.entry) if isinstance(k, dict) else (k[2] if len(k) > 2 else sig.entry)) for k in kl]
                l = [float(k.get("l", sig.entry) if isinstance(k, dict) else (k[3] if len(k) > 3 else sig.entry)) for k in kl]
                v = [float(k.get("v", 1000.0) if isinstance(k, dict) else (k[5] if len(k) > 5 else 1000.0)) for k in kl]
            else:
                c = [sig.entry] * 20
                h = [sig.entry * 1.001] * 20
                l = [sig.entry * 0.999] * 20
                v = [1000.0] * 20

            pre_score = await scoring.calculate(
                sig.symbol, sig.direction, c, h, l, v, self.client
            )
            if not pre_score["aprovado"]:
                # Log detalhado mostrando o que bloqueou
                det = pre_score.get("detalhes", {})
                tec = pre_score.get("tecnico", 0)
                of  = pre_score.get("orderflow", 0)
                mac = pre_score.get("macro", 0)
                news= pre_score.get("news_mod", 0)
                log.info(
                    f"[{sig.symbol}] Pré-trade REPROVADO {pre_score['total']}/100 "
                    f"(TEC={tec} OF={of} MAC={mac} NEWS={news:+d}) "
                    f"mínimo={scoring.MIN_SCORE}"
                )
                return

            # Salva snapshot de mercado
            await db.save_snapshot(
                sig.symbol,
                pre_score.get("oi", 0),
                pre_score.get("funding", 0),
                pre_score.get("cvd", 0),
            )

            side = "Buy" if sig.direction == "LONG" else "Sell"

            # ── Validação de parâmetros antes de enviar à API ─────
            info      = self.instruments.get(sig.symbol, {})
            qty_step  = float(info.get("qtyStep",  0.001))
            tick_size = float(info.get("tickSize", 0.01))
            min_qty   = float(info.get("minQty",   0.001))
            min_not   = float(info.get("minNotional", 1.0))

            # Validar qty
            if qty < min_qty:
                log.error(
                    f"❌ _open {sig.symbol}: qty={qty} < minQty={min_qty} — abortando"
                )
                return
            if qty * sig.entry < min_not:
                log.error(
                    f"❌ _open {sig.symbol}: notional={qty * sig.entry:.4f} < minNotional={min_not} — abortando"
                )
                return

            # Validar SL/TP — devem estar no lado correto da entrada
            if sig.sl <= 0 or sig.tp <= 0:
                log.error(
                    f"❌ _open {sig.symbol}: SL={sig.sl} ou TP={sig.tp} inválido (≤ 0) — abortando"
                )
                return
            if sig.direction == "LONG":
                if sig.sl >= sig.entry:
                    log.error(
                        f"❌ _open {sig.symbol} LONG: SL={sig.sl:.6f} >= entry={sig.entry:.6f} — abortando"
                    )
                    return
                if sig.tp <= sig.entry:
                    log.error(
                        f"❌ _open {sig.symbol} LONG: TP={sig.tp:.6f} <= entry={sig.entry:.6f} — abortando"
                    )
                    return
            else:  # SHORT
                if sig.sl <= sig.entry:
                    log.error(
                        f"❌ _open {sig.symbol} SHORT: SL={sig.sl:.6f} <= entry={sig.entry:.6f} — abortando"
                    )
                    return
                if sig.tp >= sig.entry:
                    log.error(
                        f"❌ _open {sig.symbol} SHORT: TP={sig.tp:.6f} >= entry={sig.entry:.6f} — abortando"
                    )
                    return

            log.info(
                f"🔎 _open {sig.symbol} {sig.direction} | "
                f"entry={sig.entry} sl={sig.sl} tp={sig.tp} | "
                f"qty={qty} qty_step={qty_step} tick={tick_size} | "
                f"notional={qty * sig.entry:.2f} min_not={min_not}"
            )

            # ── Retry com backoff exponencial (3 tentativas) ─────
            MAX_RETRIES   = 3
            RETRY_DELAYS  = [1.0, 2.0, 4.0]   # segundos entre tentativas
            last_exc: Exception | None = None

            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    log.info(
                        f"📡 _open {sig.symbol} tentativa {attempt}/{MAX_RETRIES} | "
                        f"side={side} qty={qty} entry={sig.entry:.6f} "
                        f"sl={sig.sl:.6f} tp={sig.tp:.6f} "
                        f"qty_step={qty_step} tick={tick_size} "
                        f"notional={qty * sig.entry:.4f} balance={self.risk.balance:.4f}"
                    )
                    await self.client.place_order(
                        symbol=sig.symbol, side=side, qty=qty,
                        sl=sig.sl, tp=sig.tp,
                        instruments=self.instruments,
                    )
                    last_exc = None
                    break   # sucesso — sai do loop de retry
                except Exception as exc:
                    last_exc = exc
                    err_str  = str(exc)

                    # Extrai retCode e retMsg da mensagem de erro estruturada
                    import re as _re
                    rc_match  = _re.search(r"Bybit\s+(\d+):\s*(.*)", err_str)
                    ret_code  = rc_match.group(1) if rc_match else "?"
                    ret_msg   = rc_match.group(2).strip() if rc_match else err_str

                    log.error(
                        f"❌ _open {sig.symbol} tentativa {attempt}/{MAX_RETRIES} FALHOU | "
                        f"retCode={ret_code} retMsg='{ret_msg}' | "
                        f"params: side={side} qty={qty} qty_step={qty_step} "
                        f"entry={sig.entry:.6f} sl={sig.sl:.6f} tp={sig.tp:.6f} "
                        f"tick={tick_size} notional={qty * sig.entry:.4f} "
                        f"balance={self.risk.balance:.4f} leverage={cfg.LEVERAGE} | "
                        f"raw_error={err_str}"
                    )

                    # Erros não-recuperáveis — não faz sentido tentar de novo
                    NON_RETRYABLE = {
                        "10001",  # parâmetro inválido
                        "10004",  # assinatura inválida
                        "110007", # saldo insuficiente
                        "110013", # qty abaixo do mínimo
                        "110017", # SL/TP inválido
                        "110025", # posição não existe
                        "110043", # alavancagem já configurada
                    }
                    if ret_code in NON_RETRYABLE:
                        log.error(
                            f"🚫 _open {sig.symbol}: retCode={ret_code} é não-recuperável "
                            f"— abortando sem retry"
                        )
                        break

                    if attempt < MAX_RETRIES:
                        delay = RETRY_DELAYS[attempt - 1]
                        log.warning(
                            f"⏳ _open {sig.symbol}: aguardando {delay}s antes da "
                            f"tentativa {attempt + 1}/{MAX_RETRIES}..."
                        )
                        await asyncio.sleep(delay)

            if last_exc is not None:
                # Todas as tentativas falharam — loga resumo final e aborta
                log.error(
                    f"💀 _open {sig.symbol}: todas as {MAX_RETRIES} tentativas falharam | "
                    f"último erro: {last_exc} | "
                    f"parâmetros finais: side={side} qty={qty} "
                    f"sl={sig.sl:.6f} tp={sig.tp:.6f} entry={sig.entry:.6f}"
                )
                return

            pos = Position(sig, qty)
            pos.pre_score = pre_score["total"]
            self.positions[sig.symbol] = pos
            # Persiste no banco
            trade_id = await db.save_trade_open(
                sig.symbol, side, sig.entry, qty,
                cfg.LEVERAGE, pre_score["total"],
            )
            self._trade_ids[sig.symbol] = trade_id
            entry_type = "BOS_BREAK" if "ENTRY:BOS_BREAK" in sig.reason else \
                         "MOMENTUM" if "ENTRY:MOMENTUM" in sig.reason else "PULLBACK"
            log.info(
                f"✅ ABERTO {sig.direction} {qty} {sig.symbol} @ ${sig.entry:.4f} "
                f"SL=${sig.sl:.4f} TP=${sig.tp:.4f} "
                f"Score={sig.score}/100 RR={sig.rr} "
                f"Tipo={entry_type} ADX={sig.reason}"
            )
            _obal = await self.client.get_balance()
            # Enriquecer sinal com TP1/TP2 se disponível
            if hasattr(sig, 'tp1') and sig.tp1 != sig.tp:
                asyncio.create_task(notify(
                    f"{'🟢🚀' if sig.direction == 'LONG' else '🔴🩸'} *SINAL — {'COMPRA (LONG)' if sig.direction == 'LONG' else 'VENDA (SHORT)'}*\n"
                    f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
                    f"📍 Par:    `{sig.symbol}`\n"
                    f"💰 Entrada: `${sig.entry:,.4f}`\n"
                    f"🛑 SL:      `${sig.sl:,.4f}` _(nível técnico)_\n"
                    f"🎯 TP1:     `${sig.tp1:,.4f}` _(50% — R:R {sig.rr1:.1f})_\n"
                    f"🏆 TP2:     `${sig.tp2:,.4f}` _(50% — R:R {sig.rr2:.1f})_\n"
                    f"🧠 Score:   `{sig.score}/100`\n"
                    f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
                    f"_SL move para break-even ao atingir TP1_"
                ))
            else:
                await notify(await signal_msg(sig))
            await notify(await order_opened_msg(sig, qty, _obal, _obal*cfg.LEVERAGE))
        except Exception as e:
            import traceback
            log.error(
                f"❌ _open {sig.symbol}: exceção inesperada — {e}\n"
                f"Parâmetros do sinal: direction={sig.direction} entry={sig.entry} "
                f"sl={sig.sl} tp={sig.tp} score={sig.score} rr={sig.rr}\n"
                f"Traceback:\n{traceback.format_exc()}"
            )

    # ── Load existing positions on startup ─────────────────────
    async def _load_existing_positions(self):
        try:
            all_pos = await self.client.get_positions()
            count = 0
            for p in all_pos:
                size = float(p.get("size", 0))
                if size <= 0:
                    continue
                sym   = p["symbol"]
                side  = p.get("side", "Buy")
                ep    = float(p.get("avgPrice", 0))
                upnl  = float(p.get("unrealisedPnl", 0))
                liq   = float(p.get("liqPrice", 0))
                direction = "LONG" if side == "Buy" else "SHORT"
                atr_est = ep * 0.007

                if direction == "LONG":
                    sl = max(liq * 1.02, ep - atr_est * 1.5) if liq > 0 else ep - atr_est * 1.5
                    tp = ep + atr_est * 3.0
                else:
                    sl = min(liq * 0.98, ep + atr_est * 1.5) if liq > 0 else ep + atr_est * 1.5
                    tp = ep - atr_est * 3.0

                sig = Signal(sym, direction, ep, sl, tp, 0.75, "Startup sync", 75)
                pos = Position(sig, size)
                pos.pnl = upnl
                cur = float(p.get("markPrice", ep))
                pos.update_pnl(cur)
                self.positions[sym] = pos
                count += 1
                log.info(f"📂 Carregada: {direction} {size} {sym} @ ${ep:.4f} PnL=${upnl:.4f}")

            if count:
                log.info(f"✅ {count} posição(ões) sincronizadas do Bybit")
        except Exception as e:
            log.error(f"_load_existing: {e}")

    async def _update_balance(self):
        """
        BUG CORRIGIDO: _dbal era NameError silenciado por except:pass
        Agora: notificação de drawdown disparada corretamente.
        """
        try:
            bal = await self.client.get_balance()
            if bal < 0:
                return
            self.risk.update(bal)
            self._recalc_daily_limits()   # atualiza meta/stop com saldo atual
            if self.risk.drawdown >= cfg.MAX_DRAWDOWN:
                log.warning(
                    f"🚨 Drawdown {self.risk.drawdown:.1%} ≥ limite "
                    f"{cfg.MAX_DRAWDOWN:.0%} → pausando entradas"
                )
                self.active = False
                _dbal = bal   # inicializa antes de usar (fix NameError)
                await notify(await drawdown_msg(self.risk.drawdown, _dbal))
        except Exception as e:
            log.error(f"_update_balance: {e}")

    # ── Status (endpoint /api/status) ──────────────────────────
    def get_status(self) -> dict:
        summaries = self.stats.all_summaries()
        return {
            "connected":        self.connected,
            "active":           self.active,
            "balance":          round(self.risk.balance, 4),
            "buying_power":     round(self.risk.balance * cfg.LEVERAGE, 2),
            "drawdown_pct":     round(self.risk.drawdown * 100, 2),
            "leverage":         cfg.LEVERAGE,
            "max_positions":    cfg.MAX_POSITIONS,
            "min_entry_score":  cfg.MIN_ENTRY_SCORE,
            "open_positions":   len(self.positions),
            "viable_symbols":   len(self.viable_symbols),
            "positions":        [p.to_dict() for p in self.positions.values()],
            "pnl_session":      summaries["session"],
            "pnl_1d":           summaries["1d"],
            "pnl_7d":           summaries["7d"],
            "pnl_30d":          summaries["30d"],
            "wins":             summaries["session"]["wins"],
            "losses":           summaries["session"]["losses"],
            "win_rate_pct":     summaries["session"]["win_rate"],
            "total_pnl":        summaries["session"]["pnl"],
            "symbols":          self.viable_symbols[:10],
            "macro_corr":       mdata.get_macro_summary(),
            # ── Meta diária
            "daily_target":     self.daily_target,
            "daily_stop_loss":  self.daily_stop_loss,
            "daily_pnl":        round(self.daily_pnl, 4),
            "daily_target_hit": self.daily_target_hit,
            "daily_stopped":    self.daily_stopped,
            "daily_progress":   round(min(self.daily_pnl / self.daily_target * 100, 100), 1) if self.daily_target else 0,
            "effective_score":  self._effective_score(),
            "mode":             "CONSERVADOR" if self.daily_target_hit else ("PARADO" if self.daily_stopped else "AGRESSIVO"),
            "paper_trade":      self.paper_trade,   # v12: visível no dashboard
            # Campos novos v11
            "daily_target_pct":    cfg.DAILY_TARGET_PCT * 100,
            "daily_stop_loss_pct": cfg.DAILY_STOP_LOSS_PCT * 100,
            "leverage":            cfg.LEVERAGE,
            "max_risk_pct":        cfg.MAX_RISK_PCT * 100,
            "correlation_matrix":  corr_guard.get_correlation_matrix(
                list(self.positions.keys()) + self.viable_symbols[:6]
            ),
        }
