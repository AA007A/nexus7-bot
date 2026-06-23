"""
BGX Capital Trading Engine v11.0
  ✅ Multi-Timeframe: 4H regime → 1H direção → 15M entrada
  ✅ Score MTF ponderado (4H:25% / 1H:30% / 15M:45%)
  ✅ Candle fechado confirmado — sem repainting
  ✅ ATR por timeframe de entrada (15M para entrada 15M)
  ✅ Regime Switching: TRENDING/RANGING/COMPRESSED
  ✅ Trailing stop progressivo (50% do alvo → ativa)
  ✅ Partial TP: fecha 50% no TP1, SL → breakeven, corre até TP2
  ✅ Circuit breaker por ativo (3 perdas consecutivas → 24h cooldown)
  ✅ Filtro de correlação entre pares (máx 1 posição por grupo)
  ✅ Filtro de sessão de mercado (penaliza altcoins em sessão ASIA)
  ✅ Meta diária: $100 lucro / $50 stop-loss (escala com saldo)
  ✅ Máximo 3 posições simultâneas (com controle de correlação)
  ✅ Score mínimo 60/100 (72 após meta diária)
  ✅ Order Flow: Spoofing, Iceberg, Agressão, CVD 4h
  ✅ Sentimento: Fear&Greed + Notícias NLP + Macro correlações
  ✅ Otimização semanal Optuna + Walk-Forward + Monte Carlo
  ✅ PostgreSQL/SQLite persistente com reconciliação de posições
  ✅ WebSocket Bybit com reconnect automático + fallback REST
  ✅ Paper Trade mode funcional (PAPER_TRADE=true)
"""
import asyncio, time, itertools, os
from datetime import datetime, timedelta
from typing import Dict, Optional, List
import numpy as np

from bot.bybit import BybitClient
from bot.strategy import Analyzer, Signal
from bot.config import cfg
from bot.logger import log
from bot.notifier import (notify, signal_msg, order_opened_msg, close_msg,
    daily_report_msg, daily_target_msg, daily_stop_msg, drawdown_msg, consecutive_losses_msg, online_msg)
from bot import database as db
from bot import score as scoring
from bot import market_data as mdata
from bot import backtest as bt
from bot.daily_tracker import DailyTracker
from bot import optimizer as opt


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
            new_sl = peak_price * (1 - cfg.TRAILING_LOCK * 0.1)
            return max(new_sl, self.sl)   # nunca recua abaixo do SL original
        else:
            peak_price = self.entry - (self.peak_pnl / self.qty if self.qty > 0 else 0)
            new_sl = peak_price * (1 + cfg.TRAILING_LOCK * 0.1)
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

    def live_metrics(self) -> dict:
        """
        Métricas quantitativas avançadas em tempo real.
        Calculadas sobre TODOS os trades da sessão.
        Expostas via /api/metrics para monitoramento de qualidade.
        """
        import itertools
        trades = self.trades
        if not trades:
            return {"status": "Sem trades na sessão"}

        rets = [
            t.pnl / (t.entry * t.qty) if t.entry * t.qty > 0 else 0
            for t in trades
        ]
        arr  = np.array(rets)
        wins    = arr[arr > 0]
        losses  = arr[arr < 0]
        total   = len(arr)

        # Expectância: ganho médio esperado por trade
        wr       = len(wins) / total if total else 0
        avg_win  = float(wins.mean())  if len(wins)  > 0 else 0.0
        avg_loss = float(losses.mean()) if len(losses) > 0 else 0.0
        expectancy = wr * avg_win + (1 - wr) * avg_loss

        # Consistência: desvio padrão dos retornos (menor = mais consistente)
        consistency = float(arr.std()) if total > 1 else 0.0

        # Sharpe simples (sem risk-free rate)
        sharpe = float(arr.mean() / arr.std()) if arr.std() > 0 and total > 1 else 0.0

        # Recovery Factor: lucro total / max drawdown
        cum   = np.cumsum(arr)
        peak  = np.maximum.accumulate(cum)
        dd    = peak - cum
        max_dd = float(dd.max()) if len(dd) > 0 else 0.0
        recovery_factor = round(float(cum[-1]) / max_dd, 2) if max_dd > 0 else float("inf")

        # Maior sequência consecutiva de perdas
        max_consec = 0
        for is_loss, group in itertools.groupby(rets, lambda x: x < 0):
            if is_loss:
                max_consec = max(max_consec, len(list(group)))

        # Profit Factor
        gp = float(wins.sum())  if len(wins)   > 0 else 0.0
        gl = float(abs(losses.sum())) if len(losses) > 0 else 1e-9
        pf = round(gp / gl, 2)

        return {
            "total_trades":       total,
            "win_rate_pct":       round(wr * 100, 1),
            "expectancy_pct":     round(expectancy * 100, 4),
            "profit_factor":      pf,
            "sharpe_ratio":       round(sharpe, 3),
            "consistency_std":    round(consistency * 100, 4),
            "max_drawdown_pct":   round(max_dd * 100, 2),
            "recovery_factor":    recovery_factor,
            "max_consec_losses":  max_consec,
            "avg_win_pct":        round(avg_win * 100, 3),
            "avg_loss_pct":       round(avg_loss * 100, 3),
            "edge_ratio":         round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else 0,
        }

    def daily_pnl(self) -> float:
        """PnL realizado apenas hoje (UTC)."""
        today = datetime.now(timezone.utc).date()
        total = 0.0
        for t in self.trades:
            if t.closed_at.date() == today:
                total += t.pnl
        return total


# ─── Risk Manager ─────────────────────────────────────────────────────────────
class RiskManager:
    def __init__(self):
        self.peak     = 0.0
        self.balance  = 0.0
        self.drawdown = 0.0
        self._ready   = False

    def init(self, balance: float):
        if not self._ready and balance > 0:
            self.peak    = balance
            self.balance = balance
            self._ready  = True
            log.info(f"📊 RiskManager: ${balance:.4f} | poder=${balance*cfg.LEVERAGE:.2f}")

    def update(self, balance: float):
        if balance <= 0:
            return
        self.balance  = balance
        self.peak     = max(self.peak, balance)
        self.drawdown = (self.peak - balance) / self.peak if self.peak > 0 else 0.0

    def can_open(self, n_open: int) -> bool:
        if not self._ready:
            return False
        if self.drawdown >= cfg.MAX_DRAWDOWN:
            log.warning(f"🚨 Drawdown {self.drawdown:.1%} >= limite {cfg.MAX_DRAWDOWN:.0%} → bloqueado")
            return False
        if n_open >= cfg.MAX_POSITIONS:
            log.info(f"⛔ {n_open}/{cfg.MAX_POSITIONS} posições abertas → aguardando fechamento")
            return False
        return True

    def size(self, symbol: str, entry: float, instruments: dict) -> float:
        """
        Calcula quantidade segura para a ordem.
        REGRA ABSOLUTA: margem usada nunca excede 95% do saldo real.
        """
        if entry <= 0 or not self._ready or self.balance <= 0:
            return 0.0

        info     = instruments.get(symbol, {})
        min_qty  = float(info.get("minQty",  0.001))
        qty_step = float(info.get("qtyStep", 0.001))
        min_not  = float(info.get("minNotional", 1.0))

        balance  = self.balance           # saldo real atual em USDT
        leverage = cfg.LEVERAGE

        # CAP ABSOLUTO: nunca usar mais de 80% do saldo como margem
        max_margin   = balance * 0.80
        max_notional = max_margin * leverage

        # Target: MAX_RISK_PCT do buying power
        target_not = balance * leverage * cfg.MAX_RISK_PCT
        
        # Aplicar cap absoluto
        target_not = min(target_not, max_notional)
        target_not = max(target_not, min_not)

        # Calcular quantidade — usar math.floor para evitar ruído de ponto flutuante
        import math
        qty   = target_not / entry
        steps = max(1, math.floor(qty / qty_step))
        qty   = round(steps * qty_step, 8)
        qty   = max(qty, min_qty)

        # Verificação HARD: margem da ordem nunca > saldo
        final_notional = qty * entry
        final_margin   = final_notional / leverage
        if final_margin > balance * 0.90:
            qty   = (balance * 0.80 * leverage) / entry
            steps = max(1, math.floor(qty / qty_step))
            qty   = round(steps * qty_step, 8)
            qty   = max(qty, min_qty)

        # Rejeitar se ainda insuficiente
        if qty <= 0 or qty * entry < min_not:
            log.warning(
                f"📐 {symbol}: saldo ${balance:.2f} insuficiente para "
                f"notional mínimo ${min_not} (entry=${entry})"
            )
            return 0.0

        log.info(
            f"📐 {symbol}: qty={qty} notional=${qty*entry:.2f} "
            f"margem=${qty*entry/leverage:.2f} / saldo=${balance:.2f}"
        )
        return qty


# ─── Trading Engine ───────────────────────────────────────────────────────────
class TradingEngine:
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
        # Parâmetros otimizados pelo Optuna (carregados do JSON se disponível)
        self._opt_params  = opt.load_optimized_params()
        self._cooldown:   Dict[str, float] = {}   # símbolo → timestamp até quando não operar
        self._consec_losses: Dict[str, int] = {}  # símbolo → perdas consecutivas

        # ── Meta diária ──────────────────────────────────────────
        self.daily_pnl        = 0.0      # PnL acumulado no dia (USDT)
        # RISK-4: meta e stop diário escalam com saldo (1% lucro / 0.5% stop)
        # Se DAILY_TARGET > 0: usa valor fixo. Se = 0: calcula dinamicamente.
        self.daily_target     = cfg.DAILY_TARGET      # $100 fixo ou recalcula no reset
        self.daily_stop_loss  = cfg.DAILY_STOP_LOSS   # $50 fixo ou recalcula no reset
        self.daily_tracker    = DailyTracker()        # usado pelo SignalProcessorMixin
        self.daily_target_hit = False    # meta batida hoje?
        self.daily_stopped    = False    # stop-loss diário ativado?
        self._last_reset_day  = -1       # último dia (UTC) que resetou

    # ── Lifecycle ──────────────────────────────────────────────
    async def run(self):
        if self._running:
            return
        self._running = True
        log.info("⚡ Engine v10 iniciando...")
        await db.init()   # inicia DB (PostgreSQL ou SQLite)
        asyncio.create_task(scoring.update_macro_cache())        # Fear&Greed
        asyncio.create_task(scoring.news_reader_loop())           # news 24/7
        asyncio.create_task(mdata.update_macro_correlations())    # DXY/S&P
        asyncio.create_task(bt.weekly_backtest_loop(self.client))   # backtest semanal
        asyncio.create_task(opt.weekly_optimization_loop(self.client)) # otimização semanal
        asyncio.create_task(self._monitor_news_pipeline())               # pipeline de notícias
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
                    await self._check_stagnation_and_invalidation()
                    await self._manage_partial_tp()
                    await self._apply_trailing_stops()
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
        self.active   = False
        self._running = False   # FIX: permite que run() seja recriado no resume
        log.info("⏸️ Bot pausado (servidor continua rodando)")

    # ── Meta diária ────────────────────────────────────────────
    def _check_daily_reset(self):
        """Reseta contadores de PnL diário à meia-noite UTC."""
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
            # RISK-4: Recalcula meta/stop com saldo atual se configurados como dinâmicos
            # cfg.DAILY_TARGET=0 → usa 1% do saldo; cfg.DAILY_STOP_LOSS=0 → usa 0.5% do saldo
            bal = self.risk.balance or 1000.0
            if cfg.DAILY_TARGET == 0:
                self.daily_target    = round(bal * 0.01, 2)   # 1% do saldo
            if cfg.DAILY_STOP_LOSS == 0:
                self.daily_stop_loss = round(bal * 0.005, 2)  # 0.5% do saldo
            log.info(f"🎯 Meta diária: ${self.daily_target:.2f} | Stop-loss dia: -${self.daily_stop_loss:.2f} | Saldo: ${bal:.2f}")

    def _update_daily_pnl(self):
        """Soma PnL realizado + não-realizado do dia e verifica meta/stop."""
        realized    = self.stats.daily_pnl()
        unrealized  = sum(p.pnl for p in self.positions.values())
        self.daily_pnl = realized + unrealized

        # ── Stop/meta via DailyTracker (diário + semanal + mensal) ───
        result = self.daily_tracker.check_limits()
        if result == 'TARGET' and not self.daily_target_hit:
            self.daily_target_hit = True
        elif result in ('STOP', 'WEEKLY_STOP', 'MONTHLY_STOP'):
            if not self.daily_stopped:
                self.daily_stopped = True
                label = {'STOP':'DIÁRIO','WEEKLY_STOP':'SEMANAL','MONTHLY_STOP':'MENSAL'}[result]
                log.warning(f"🛑 STOP-LOSS {label} ATINGIDO: ${self.daily_pnl:.2f}")
                asyncio.create_task(notify(
                    f"🛑 *Stop-Loss {label}*\n"
                    f"PnL: `${self.daily_pnl:.2f}` → bot pausado"
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

                    # ── Circuit breaker individual por ativo ───────────
                    await self._record_trade_result(sym, pnl_net)
                    # ── Journal estruturado com metadados ─────────────
                    rr_achieved = abs(pnl_net / max(abs(pos.entry - pos.sl), 0.0001) / pos.qty) if pos.sl else 0
                    self.daily_tracker.add_pnl(
                        pnl_net,
                        symbol=sym,
                        entry_type=getattr(pos, "entry_type", ""),
                        regime=getattr(pos, "regime", ""),
                        session=self._get_market_session(),
                        rr_achieved=round(rr_achieved, 2),
                    )

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
            now_h = __import__("datetime").datetime.utcnow().hour
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


    # ── Configuração do circuit breaker por ativo ─────────────────────────
    _MAX_CONSEC_LOSSES: int = int(os.environ.get('MAX_CONSEC_LOSSES', '3'))  # configurável via env var
    _CB_COOLDOWN_HOURS:  int = int(os.environ.get('CB_COOLDOWN_HOURS',  '24')) # configurável via env var

    async def _record_trade_result(self, symbol: str, pnl: float):
        """
        Registra resultado de um trade e ativa circuit breaker
        se o símbolo atingir MAX_CONSEC_LOSSES perdas consecutivas.

        Circuit breaker individual: mais cirúrgico que o drawdown global.
        Permite continuar operando outros pares enquanto um par problemático
        fica em cooldown de 24h.
        """
        if pnl < 0:
            self._consec_losses[symbol] = self._consec_losses.get(symbol, 0) + 1
            count = self._consec_losses[symbol]

            if count >= self._MAX_CONSEC_LOSSES:
                cooldown_until = time.time() + self._CB_COOLDOWN_HOURS * 3600
                self._cooldown[symbol] = cooldown_until
                log.warning(
                    f"🚫 [{symbol}] Circuit breaker ativado: "
                    f"{count} perdas consecutivas → "
                    f"cooldown de {self._CB_COOLDOWN_HOURS}h"
                )
                await notify(
                    f"🚫 *Circuit Breaker — {symbol}*\n"
                    f"`{count}` perdas consecutivas\n"
                    f"Cooldown: `{self._CB_COOLDOWN_HOURS}h`\n"
                    f"Retoma às: `{__import__('datetime').datetime.utcfromtimestamp(cooldown_until).strftime('%H:%M UTC')}`"
                )
        else:
            # Reset após lucro
            if self._consec_losses.get(symbol, 0) > 0:
                log.info(
                    f"✅ [{symbol}] Trade lucrativo — "
                    f"reset de perdas consecutivas "
                    f"({self._consec_losses[symbol]} → 0)"
                )
            self._consec_losses[symbol] = 0


    async def _check_stagnation_and_invalidation(self):
        """
        Saída por tempo: fecha posição se em 4h o preço não se moveu > 0.5x ATR.
        Saída por invalidação: fecha se CHoCH oposto aparece após a entrada.
        Saída por regime: fecha se regime mudou para RANGING/COMPRESSED/CHOPPY.
        Itens 8, 9, 15 da lista de melhorias.
        """
        from bot.indicators import atr as calc_atr
        from bot.strategy import detect_regime

        for sym, pos in list(self.positions.items()):
            try:
                k15 = self.client.get_kline_cache(sym, "15") or []
                if len(k15) < 20:
                    continue

                closes = [float(k["c"]) for k in k15[:-1]]
                highs  = [float(k["h"]) for k in k15[:-1]]
                lows   = [float(k["l"]) for k in k15[:-1]]
                cur    = pos.current_price or closes[-1]

                atr_val = float(calc_atr(highs, lows, closes, 14)[-1])
                if atr_val <= 0:
                    continue

                # ── Saída por TEMPO ────────────────────────────────
                # Fecha se após 4h (16 candles 15M) o preço não se moveu > 0.5x ATR
                STAGNATION_BARS  = 16
                STAGNATION_MULT  = 0.5
                movement = abs(cur - pos.entry)
                bars_open = len(k15)  # proxy de tempo em trade
                if (bars_open >= STAGNATION_BARS
                        and movement < atr_val * STAGNATION_MULT):
                    log.info(
                        f"⏱️  [{sym}] Saída por TEMPO: {bars_open} candles aberto, "
                        f"movimento={movement:.4f} < {atr_val*STAGNATION_MULT:.4f} "
                        f"(0.5×ATR) → fechando para evitar funding acumulado"
                    )
                    close_side = "Sell" if pos.direction == "LONG" else "Buy"
                    await self.client.place_order(
                        symbol=sym, side=close_side,
                        qty=pos.qty, sl=0, tp=0,
                        instruments=self.instruments
                    )
                    continue

                # ── Saída por INVALIDAÇÃO (CHoCH oposto) ──────────
                # Se após entrada LONG aparece CHoCH de baixa → setup invalidado
                if len(closes) >= 10:
                    recent_c = closes[-10:]
                    recent_h = highs[-10:]
                    recent_l = lows[-10:]

                    # CHoCH simples: HH seguido de LL (topos e fundos)
                    choch_bear = (
                        recent_h[-1] < recent_h[-3] and
                        recent_l[-1] < recent_l[-3] and
                        recent_c[-1] < recent_c[-3]
                    )
                    choch_bull = (
                        recent_l[-1] > recent_l[-3] and
                        recent_h[-1] > recent_h[-3] and
                        recent_c[-1] > recent_c[-3]
                    )

                    invalidated = (
                        (pos.direction == "LONG"  and choch_bear) or
                        (pos.direction == "SHORT" and choch_bull)
                    )

                    if invalidated and not pos.tp1_hit:
                        log.info(
                            f"❌ [{sym}] Saída por INVALIDAÇÃO: CHoCH oposto detectado "
                            f"após entrada {pos.direction} → fechando antes do SL"
                        )
                        close_side = "Sell" if pos.direction == "LONG" else "Buy"
                        await self.client.place_order(
                            symbol=sym, side=close_side,
                            qty=pos.qty, sl=0, tp=0,
                            instruments=self.instruments
                        )
                        continue

                # ── Saída por MUDANÇA DE REGIME ────────────────────
                # Se o regime mudou para RANGING/COMPRESSED/CHOPPY após a entrada
                k4h = self.client.get_kline_cache(sym, "240") or []
                if len(k4h) >= 20:
                    c4h   = [float(k["c"]) for k in k4h[:-1]]
                    h4h   = [float(k["h"]) for k in k4h[:-1]]
                    l4h   = [float(k["l"]) for k in k4h[:-1]]
                    atr4h = float(calc_atr(h4h, l4h, c4h, 14)[-1])
                    regime_now = detect_regime(c4h, h4h, l4h, atr4h)

                    if regime_now in ("RANGING", "COMPRESSED", "CHOPPY") and not pos.tp1_hit:
                        log.info(
                            f"🔄 [{sym}] Saída por REGIME: mercado mudou para "
                            f"{regime_now} → setup trend-follow inválido, fechando"
                        )
                        close_side = "Sell" if pos.direction == "LONG" else "Buy"
                        await self.client.place_order(
                            symbol=sym, side=close_side,
                            qty=pos.qty, sl=0, tp=0,
                            instruments=self.instruments
                        )

            except Exception as e:
                log.error(f"_check_stagnation_and_invalidation {sym}: {e}")

    async def _manage_partial_tp(self):
        """
        Partial Take Profit: fecha 50% da posição ao atingir TP1,
        move SL para breakeven e deixa os 50% restantes correrem até TP2.

        TP1 = entry ± 1× risco (1:1 R:R) — captura rápida
        TP2 = tp original    — alvo final com trailing

        RISK-2: TP1 inclui custo de funding estimado (8h × taxa média 0.01%)
        para evitar fechar "lucrativo" com funding negativo acumulado.

        Benefício: garante lucro parcial, elimina risco de breakeven,
        melhora consistência do win rate ajustado por expectativa.
        """
        for sym, pos in list(self.positions.items()):
            try:
                if pos.tp1_hit:
                    continue   # já executou o parcial

                cur = pos.current_price
                if not cur or cur <= 0:
                    continue

                # TP1 = entry ± distância do SL (1:1 R:R)
                # RISK-2: adiciona custo estimado de funding (8h × 0.01% = 0.08%)
                # para garantir que partial TP seja genuinamente lucrativo
                risk_dist    = abs(pos.entry - pos.sl)
                if risk_dist <= 0:
                    continue
                funding_cost = pos.entry * 0.0001 * 3  # 3 períodos de 8h = 0.03%
                tp1_long  = pos.entry + risk_dist + funding_cost
                tp1_short = pos.entry - risk_dist - funding_cost
                tp1_price = tp1_long if pos.direction == "LONG" else tp1_short

                # Verificar se TP1 foi atingido
                tp1_hit = (
                    (pos.direction == "LONG"  and cur >= tp1_price) or
                    (pos.direction == "SHORT" and cur <= tp1_price)
                )
                if not tp1_hit:
                    continue

                # Calcular qty parcial (50% da posição original)
                # RISK-3: respeita qty_step do instrumento para evitar rejeição
                raw_partial = pos.qty_original * 0.5
                qty_step = 0.001  # fallback conservador
                if self.instruments:
                    inst = self.instruments.get(sym, {})
                    qty_step = float(inst.get("lotSizeFilter", {}).get("qtyStep", 0.001))
                if qty_step > 0:
                    partial_qty = round(raw_partial - (raw_partial % qty_step), len(str(qty_step).rstrip("0").split(".")[-1]))
                else:
                    partial_qty = round(raw_partial, 4)
                if partial_qty <= 0 or partial_qty > pos.qty:
                    continue

                # Fechar 50% da posição
                close_side = "Sell" if pos.direction == "LONG" else "Buy"
                result = await self.client.place_order(
                    symbol=sym, side=close_side,
                    qty=partial_qty, sl=0, tp=0,
                    instruments=self.instruments,
                )

                # Mover SL para breakeven
                await self.client.set_sl(sym, pos.entry)

                # Atualizar estado da posição
                pnl_partial = risk_dist * partial_qty   # PnL gross do parcial
                fee_p = partial_qty * cur * 0.00055 * 2
                pnl_net = pnl_partial - fee_p

                pos.tp1_hit     = True
                pos.sl          = pos.entry   # SL no breakeven
                pos.trailing_sl = pos.entry
                pos.qty         = pos.qty - partial_qty   # atualiza qty restante

                log.info(
                    f"✂️  [{sym}] Partial TP1: fechou {partial_qty} @ {cur:.6f} "
                    f"| PnL parcial: ${pnl_net:.2f} "
                    f"| SL → breakeven {pos.entry:.6f} "
                    f"| Restante: {pos.qty:.4f} até TP2={pos.tp:.6f}"
                )
                await notify(
                    f"✂️ *Partial TP1 — {sym}*\n"
                    f"Fechou 50% @ `{cur:.4f}`\n"
                    f"PnL parcial: `${pnl_net:.2f}`\n"
                    f"SL movido para breakeven\n"
                    f"Restante correndo até TP2 `{pos.tp:.4f}`"
                )
            except Exception as e:
                log.error(f"_manage_partial_tp {sym}: {e}")

    async def _apply_trailing_stops(self):
        """
        Trailing Stop progressivo.
        Ativa quando lucro >= 50% do alvo (cfg.TRAILING_TRIGGER).
        Trava 25% * 10% = 2.5% abaixo do pico (cfg.TRAILING_LOCK * 0.1).
        Nunca recua abaixo do SL original — protege capital sem cortar early.
        Move o SL na exchange via /v5/position/trading-stop.
        """
        for sym, pos in list(self.positions.items()):
            try:
                # Atualiza PnL com preço atual
                cur = pos.current_price
                if not cur or cur <= 0:
                    continue
                pos.update_pnl(cur)

                # Calcula novo SL via método da Position
                new_sl = pos.calc_trailing_sl()
                if new_sl is None:
                    continue

                # Só move se o SL melhorou (LONG: sobe, SHORT: desce)
                improved = (
                    (pos.direction == "LONG"  and new_sl > pos.trailing_sl) or
                    (pos.direction == "SHORT" and new_sl < pos.trailing_sl)
                )
                if not improved:
                    continue

                old_sl = pos.trailing_sl

                # Move SL na exchange
                await self.client.set_sl(sym, new_sl)
                pos.trailing_sl = new_sl
                pos.sl          = new_sl   # mantém sl e trailing_sl sincronizados

                log.info(
                    f"🔒 [{sym}] Trailing SL: {old_sl:.6f} → {new_sl:.6f} "
                    f"| preço={cur:.6f} pnl=${pos.pnl:.2f} "
                    f"(ativo={pos.trailing_active})"
                )
            except Exception as e:
                log.error(f"_apply_trailing_stops {sym}: {e}")

    # ── Fecha posição quando lucro = 2x o risco (R:R dobrado) ──
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
                    fee_open  = pos.qty * pos.entry * 0.00055
                    fee_close = pos.qty * price     * 0.00055
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

                    # ── Circuit breaker individual por ativo ───────────
                    await self._record_trade_result(sym, pnl_net)
                    # ── Journal estruturado com metadados ─────────────
                    rr_achieved = abs(pnl_net / max(abs(pos.entry - pos.sl), 0.0001) / pos.qty) if pos.sl else 0
                    self.daily_tracker.add_pnl(
                        pnl_net,
                        symbol=sym,
                        entry_type=getattr(pos, "entry_type", ""),
                        regime=getattr(pos, "regime", ""),
                        session=self._get_market_session(),
                        rr_achieved=round(rr_achieved, 2),
                    )

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
    # ── Grupos de correlação — pares com beta > 0.8 entre si ─────────
    # Limite: no máximo 1 posição aberta por grupo simultaneamente
    _CORR_GROUPS: list = [
        {"BTCUSDT", "ETHUSDT"},              # BTC e ETH: correlação ~0.92
        {"SOLUSDT", "AVAXUSDT", "DOTUSDT"},  # L1 alternativos: correlação ~0.88
        {"BNBUSDT"},                          # BNB: isolado (exchange token)
        {"XRPUSDT", "ADAUSDT"},              # pagamentos/contratos: correlação ~0.85
        {"DOGEUSDT", "MATICUSDT"},           # meme/polygon: correlação ~0.80
        {"LINKUSDT", "LTCUSDT"},             # oráculos/store-of-value
    ]


    # ── Sessões de mercado e penalidades por par ──────────────────────────
    # Cripto tem comportamento diferente por sessão:
    # ASIA (00-08 UTC): volume baixo, altcoins fracas, BTC/ETH ok
    # LONDON (08-16 UTC): tendências se formam, liquidez crescente
    # NEW_YORK (16-24 UTC): maior volume, breakouts mais confiáveis
    _SESSION_PENALTY: dict = {
        "ASIA":     {"SOLUSDT": -8, "BNBUSDT": -8, "XRPUSDT": -5,
                     "DOGEUSDT": -10, "MATICUSDT": -8, "AVAXUSDT": -8},
        "LONDON":   {},   # sem penalidades — boa sessão para todos
        "NEW_YORK": {},   # sem penalidades — melhor sessão para breakouts
    }


    # ── Regime Switching: parâmetros por regime de mercado ────────────────
    # Comportamento diferente para cada condição de mercado:
    #   TRENDING_UP/DOWN: score relaxado, direção bloqueada, TP maior
    #   RANGING:          score alto, ambas direções, TP menor (mean reversion)
    #   COMPRESSED:       não opera — aguarda breakout (bloqueado no analyze_mtf)
    _REGIME_PARAMS: dict = {
        "TRENDING_UP": {
            "min_score":      55,    # entrada mais fácil com tendência
            "allowed_sides":  ["LONG"],   # só long — não nadar contra a maré
            "sl_mult_adj":    +0.2,  # SL um pouco mais largo em tendência
            "tp_mult_adj":    +0.5,  # TP maior — tendências andam mais
            "score_bonus":    +5,    # bônus de score por estar na direção certa
        },
        "TRENDING_DOWN": {
            "min_score":      55,
            "allowed_sides":  ["SHORT"],
            "sl_mult_adj":    +0.2,
            "tp_mult_adj":    +0.5,
            "score_bonus":    +5,
        },
        "RANGING": {
            "min_score":      72,    # exige score alto — sem tendência, mais falsos
            "allowed_sides":  ["LONG", "SHORT"],
            "sl_mult_adj":    -0.2,  # SL mais apertado em range
            "tp_mult_adj":    -0.8,  # TP menor — range não anda muito
            "score_bonus":    -5,    # penalidade por mercado lateral
        },
        "COMPRESSED": {
            "min_score":      999,   # nunca opera (bloqueado)
            "allowed_sides":  [],
            "sl_mult_adj":    0.0,
            "tp_mult_adj":    0.0,
            "score_bonus":    0,
        },
    }

    def _get_regime_params(self, regime: str) -> dict:
        """Retorna parâmetros ajustados para o regime atual."""
        return self._REGIME_PARAMS.get(regime, self._REGIME_PARAMS["RANGING"])

    def _regime_allows_direction(self, regime: str, direction: str) -> bool:
        """Verifica se o regime atual permite a direção do sinal."""
        rp = self._get_regime_params(regime)
        allowed = rp.get("allowed_sides", ["LONG", "SHORT"])
        if direction not in allowed:
            log.info(
                f"[Regime {regime}] Direção {direction} bloqueada "
                f"— apenas {allowed} permitido neste regime"
            )
            return False
        return True

    @staticmethod
    def _get_market_session() -> str:
        """Retorna a sessão de mercado atual com base no horário UTC."""
        hour = datetime.now(timezone.utc).hour
        if 0 <= hour < 8:
            return "ASIA"
        elif 8 <= hour < 16:
            return "LONDON"
        else:
            return "NEW_YORK"

    def _session_score_adjustment(self, symbol: str, base_score: int) -> int:
        """
        Ajusta o score de entrada com base na sessão de mercado.
        Penaliza altcoins de baixa liquidez na sessão asiática.
        Retorna score ajustado (nunca abaixo de 0).
        """
        session = self._get_market_session()
        penalty = self._SESSION_PENALTY.get(session, {}).get(symbol, 0)
        if penalty != 0:
            log.debug(
                f"[{symbol}] Sessão {session}: "
                f"score {base_score} {penalty:+d} = {base_score + penalty}"
            )
        return max(0, base_score + penalty)

    def _correlation_allows(self, symbol: str) -> bool:
        """
        Retorna True se é seguro abrir posição em 'symbol'.
        Regra: máximo 1 posição aberta por grupo de correlação.
        Isso garante que MAX_POSITIONS=3 representa 3 apostas DISTINTAS,
        não 3x a mesma aposta direcional em cripto.
        """
        for group in self._CORR_GROUPS:
            if symbol not in group:
                continue
            # Verifica se já existe posição aberta em outro membro do grupo
            for open_sym in self.positions:
                if open_sym != symbol and open_sym in group:
                    log.info(
                        f"[{symbol}] Bloqueado por correlação: "
                        f"{open_sym} já aberto no mesmo grupo {group}"
                    )
                    return False
        return True

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
            # Filtro de correlação: não abre posição em par do mesmo grupo
            if not self._correlation_allows(sym):
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
                if sig:
                    # ── Ajuste de sessão de mercado ──────────────
                    adjusted = self._session_score_adjustment(sym, sig.score)
                    if adjusted < min_score:
                        log.info(
                            f"[{sym}] Score {sig.score}→{adjusted} após "
                            f"ajuste sessão {self._get_market_session()} → HOLD"
                        )
                        continue
                    sig.score = adjusted

                    # ── Regime Switching: filtro de direção ───────
                    # O regime é detectado no 4H pelo analyze_mtf.
                    # Aqui bloqueamos direções proibidas pelo regime atual.
                    regime_from_sig = getattr(sig, "regime", "RANGING")
                    if not self._regime_allows_direction(regime_from_sig, sig.direction):
                        continue
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
                        def ga(kl): return ([k["c"] for k in kl],[k["h"] for k in kl],[k["l"] for k in kl],[k["o"] for k in kl],[k["v"] for k in kl])
                        c15,h15,l15,o15,v15 = ga(k15)
                        c1h,h1h,l1h,o1h,v1h = ga(k1h)
                        c4h,h4h,l4h,o4h,v4h = ga(k4h)
                        def get_atr(h,l,c):
                            a=atr_fn(h,l,c); return a[-1], float(np.mean(a[-20:])) if len(a)>=20 else a[-1]
                        av15,ag15=get_atr(h15,l15,c15)
                        av1h,ag1h=get_atr(h1h,l1h,c1h)
                        av4h,ag4h=get_atr(h4h,l4h,c4h)
                        e20_4h=__import__('bot.indicators',fromlist=['ema']).ema(c4h,20)[-1]
                        e50_4h=__import__('bot.indicators',fromlist=['ema']).ema(c4h,50)[-1]
                        e20_1h=__import__('bot.indicators',fromlist=['ema']).ema(c1h,20)[-1]
                        e50_1h=__import__('bot.indicators',fromlist=['ema']).ema(c1h,50)[-1]
                        bull_4h = not __import__('numpy').isnan(e20_4h) and e20_4h>e50_4h and c4h[-1]>e20_4h
                        bear_4h = not __import__('numpy').isnan(e20_4h) and e20_4h<e50_4h and c4h[-1]<e20_4h
                        bull_1h = not __import__('numpy').isnan(e20_1h) and e20_1h>e50_1h and c1h[-1]>e20_1h
                        bear_1h = not __import__('numpy').isnan(e20_1h) and e20_1h<e50_1h and c1h[-1]<e20_1h
                        direction = "LONG" if (bull_4h or bull_1h) else "SHORT"
                        s4=score_tf(c4h,h4h,l4h,o4h,v4h,direction,av4h,ag4h)
                        s1=score_tf(c1h,h1h,l1h,o1h,v1h,direction,av1h,ag1h)
                        s15=score_tf(c15,h15,l15,o15,v15,direction,av15,ag15)
                        combined=round(s4["total"]*0.30+s1["total"]*0.30+s15["total"]*0.40)
                        regime=detect_regime(c4h,h4h,l4h,av4h)
                        from bot.indicators import rsi as rsi_fn
                        rsi_v=rsi_fn(c15)[-1]
                        vols=__import__('numpy').array(v15); avg_vol=vols[-21:-1].mean() if len(vols)>21 else vols.mean() or 1
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
            # Atualizar saldo real antes de calcular qty
            fresh_bal = await self.client.get_balance()
            if fresh_bal > 0:
                self.risk.update(fresh_bal)
            qty = self.risk.size(sig.symbol, sig.entry, self.instruments)
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
        try:
            bal = await self.client.get_balance()
            if bal >= 0:
                self.risk.update(bal)
                _dbal = bal   # FIX: sempre definida, independente do drawdown
                if self.risk.drawdown >= cfg.MAX_DRAWDOWN:
                    log.warning(f"🚨 Drawdown {self.risk.drawdown:.1%} ≥ limite → pausando entradas")
                    self.active = False
                    _dbal = await self.client.get_balance()
                await notify(await drawdown_msg(self.risk.drawdown, _dbal))
        except Exception:
            pass

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
        }
