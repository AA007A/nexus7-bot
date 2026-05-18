"""
KAKAZITO TRADE Trading Engine v7.1
  ✅ Meta diária de $100 de lucro
  ✅ Stop-loss diário de $50 (para tudo se perder $50 no dia)
  ✅ Modo agressivo até bater a meta → modo conservador depois
  ✅ Máximo 3 posições simultâneas
  ✅ Score mínimo 80/100 para entrar (88 após bater a meta)
  ✅ Trailing stop progressivo
  ✅ Sincronização em tempo real com Bybit
"""
import asyncio, time
from datetime import datetime, timedelta
from typing import Dict, Optional, List

from bot.bybit import BybitClient
from bot.strategy import Analyzer, Signal
from bot.config import cfg
from bot.logger import log
from bot.notifier import notify, signal_msg


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
        TRAILING SL DESATIVADO.
        SL permanece fixo no nível técnico original.
        Trade só fecha por: TP atingido, SL técnico, ou R:R dobrado.
        """
        return None

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
        if entry <= 0 or not self._ready:
            return 0.0
        info     = instruments.get(symbol, {})
        min_qty  = info.get("minQty",  0.001)
        qty_step = info.get("qtyStep", 0.001)
        min_not  = info.get("minNotional", 1.0)

        buying_power = self.balance * cfg.LEVERAGE
        # Usa risco reduzido após meta diária ser batida
        risk_pct = engine_ref.get("risk_pct", cfg.MAX_RISK_PCT) if hasattr(cfg,"_engine_ref") else cfg.MAX_RISK_PCT
        target_not   = buying_power * cfg.MAX_RISK_PCT
        target_not   = max(target_not, min_not)

        if target_not > buying_power * 0.95:
            target_not = buying_power * 0.95

        qty   = target_not / entry
        steps = int(qty / qty_step)
        qty   = round(steps * qty_step, 8)
        qty   = max(qty, min_qty)

        if qty * entry > buying_power:
            qty   = (buying_power * 0.90) / entry
            steps = int(qty / qty_step)
            qty   = round(steps * qty_step, 8)
            qty   = max(qty, min_qty)

        log.info(f"📐 {symbol}: qty={qty} notional=${qty*entry:.2f}")
        return qty


# ─── Trading Engine ───────────────────────────────────────────────────────────
class TradingEngine:
    def __init__(self, client: BybitClient):
        self.client       = client
        self.analyzer     = Analyzer()
        self.risk         = RiskManager()
        self.stats        = Stats()
        self.positions:   Dict[str, Position] = {}
        self.instruments: dict = {}
        self.viable_symbols: List[str] = []
        self.connected    = False
        self.active       = False
        self._running     = False
        self._scan_idx    = 0
        self._cooldown:   Dict[str, float] = {}   # símbolo → timestamp até quando não operar

        # ── Meta diária ──────────────────────────────────────────
        self.daily_pnl        = 0.0      # PnL acumulado no dia (USDT)
        self.daily_target     = cfg.DAILY_TARGET      # $100
        self.daily_stop_loss  = cfg.DAILY_STOP_LOSS   # -$50
        self.daily_target_hit = False    # meta batida hoje?
        self.daily_stopped    = False    # stop-loss diário ativado?
        self._last_reset_day  = -1       # último dia (UTC) que resetou

    # ── Lifecycle ──────────────────────────────────────────────
    async def run(self):
        if self._running:
            return
        self._running = True
        log.info("⚡ Engine v7.0 iniciando...")
        await self._connect()

        while self._running:
            try:
                if not self.connected:
                    await asyncio.sleep(30)
                    await self._connect()
                    continue

                if self.active:
                    self._check_daily_reset()            # reseta contadores à meia-noite UTC
                    await self._update_balance()
                    await self._sync_positions()         # sincroniza com Bybit
                    await self._apply_trailing_stops()   # trailing desativado (pass)
                    await self._check_rr_double()        # fecha se R:R dobrou (lucro 2x risco)
                    self._update_daily_pnl()             # atualiza PnL do dia
                    if self.daily_stopped:
                        log.warning("🛑 Stop-loss diário ativado — aguardando próximo dia")
                    elif self.risk.can_open(len(self.positions)):
                        await self._scan_all_and_enter() # escaneia e entra nos melhores

                await asyncio.sleep(15)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Engine loop: {e}")
                await asyncio.sleep(10)

    def stop(self):
        self.active = False
        log.info("⏸️ Bot pausado (servidor continua rodando)")

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
            log.info(f"🎯 Meta diária: ${self.daily_target:.0f} | Stop-loss dia: -${self.daily_stop_loss:.0f}")

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
            import asyncio
            asyncio.create_task(notify(
                "🛑 *STOP-LOSS DIÁRIO*\n"
                f"Perda do dia: `${self.daily_pnl:.4f}`\n"
                f"Limite: `-${self.daily_stop_loss:.0f}`\n"
                "Bot pausado até meia-noite UTC"
            ))

        # ── Meta batida ───────────────────────────────────────────
        if not self.daily_target_hit and self.daily_pnl >= self.daily_target:
            self.daily_target_hit = True
            log.info(
                f"🎯 META DIÁRIA BATIDA! PnL=${self.daily_pnl:.4f} ≥ ${self.daily_target:.0f}"
                f" — Entrando em modo CONSERVADOR (score ≥ {cfg.POST_TARGET_SCORE})"
            )
            import asyncio
            asyncio.create_task(notify(
                "🎯 *META DIÁRIA BATIDA!*\n"
                f"Lucro do dia: `+${self.daily_pnl:.4f}`\n"
                f"Meta: `${self.daily_target:.0f}`\n"
                "Modo: *CONSERVADOR* ativado\n"
                f"Proximas entradas: score >= {cfg.POST_TARGET_SCORE}/100"
            ))

    def _effective_score(self) -> int:
        """Score mínimo efetivo — aumenta após bater a meta."""
        if self.daily_target_hit:
            return cfg.POST_TARGET_SCORE  # mais seletivo (88)
        return cfg.MIN_ENTRY_SCORE        # padrão (80)

    def _effective_risk_pct(self) -> float:
        """Risco por trade — reduz após bater a meta."""
        if self.daily_target_hit:
            return cfg.POST_TARGET_RISK   # conservador (15%)
        return cfg.MAX_RISK_PCT           # padrão (30%)

    # ── Connect ────────────────────────────────────────────────
    async def _connect(self):
        try:
            if not await self.client.ping():
                log.error("❌ Bybit ping falhou")
                self.connected = False
                return

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
                await asyncio.sleep(0.1)

            await self._load_existing_positions()
            self.connected = True
            self.active    = True
            log.info(f"✅ Conectado! ${bal:.4f} USDT | {len(self.viable_symbols)} pares | max {cfg.MAX_POSITIONS} posições | score >= {cfg.MIN_ENTRY_SCORE}")
            await notify(
                f"✅ *KAKAZITO TRADE v7 Online!*\n"
                f"Saldo: `${bal:.4f} USDT`\n"
                f"Poder: `${bal*cfg.LEVERAGE:.2f} USDT`\n"
                f"Max posições: `{cfg.MAX_POSITIONS}`\n"
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
                    del self.positions[sym]
                    self._cooldown[sym] = time.time() + 1800
                    icon = "✅" if pnl_net >= 0 else "❌"
                    log.info(
                        f"📭 {sym} fechado | Bruto=${pnl_gross:+.4f} "
                        f"Taxas=-${total_fee:.4f} | Líquido=${pnl_net:+.4f}"
                    )
                    await notify(
                        f"{icon} *{sym} fechado*\n"
                        f"Direção: `{pos.direction}`\n"
                        f"PnL Bruto: `${pnl_gross:+.4f}`\n"
                        f"Taxas: `-${total_fee:.4f}`\n"
                        f"PnL Líquido: `${pnl_net:+.4f}`\n"
                        f"Score: `{pos.score}/100`\n"
                        f"Sessão: `${self.stats.summary()['pnl']:+.4f}`"
                    )
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
    async def _apply_trailing_stops(self):
        """
        Trailing SL desativado por configuração do usuário.
        SL permanece no nível técnico original.
        Trade fecha somente por: TP, SL técnico ou R:R dobrado.
        """
        pass

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
                    fee_open  = pos.qty * pos.entry * 0.00055
                    fee_close = pos.qty * price     * 0.00055
                    trade = Trade(
                        sym, pos.direction, pos.entry, price,
                        pos.qty, pnl_gross, pos.opened_at,
                        fee_open=fee_open, fee_close=fee_close,
                    )
                    self.stats.add(trade)
                    del self.positions[sym]
                    self._cooldown[sym] = time.time() + 1800
                    await notify(
                        f"🎯 *{sym} — R:R DOBRADO*\n"
                        f"Direção: `{pos.direction}`\n"
                        f"Entrada: `${pos.entry:.4f}`\n"
                        f"Saída: `${price:.4f}`\n"
                        f"R:R: `{rr_atual:.2f}`\n"
                        f"PnL Bruto: `+${pnl_gross:.4f}`\n"
                        f"Taxas: `-${fee_open+fee_close:.4f}`\n"
                        f"PnL Líquido: `+${pnl_gross-fee_open-fee_close:.4f}`"
                    )
            except Exception as e:
                log.error(f"_check_rr_double {sym}: {e}")

    # ── Scan & Enter ────────────────────────────────────────────
    async def _scan_all_and_enter(self):
        """
        Multi-Timeframe scan: busca 15m E 1h para cada símbolo.
        Só entra quando AMBOS os timeframes apontam na mesma direção.
        """
        candidates = []
        min_score = self._effective_score()

        for sym in self.viable_symbols:
            if sym in self.positions:
                continue
            cooldown_left = self._cooldown.get(sym, 0) - time.time()
            if cooldown_left > 0:
                log.debug(f"[{sym}] cooldown {cooldown_left/60:.0f}min → skip")
                continue
            try:
                # Busca 3 timeframes: 4H (tendência), 1H (confirmação), 15M (entrada)
                k15, k1h, k4h = await asyncio.gather(
                    self.client.get_klines(sym, "15",  100),
                    self.client.get_klines(sym, "60",  100),
                    self.client.get_klines(sym, "240", 100),
                )
                if len(k15) < 60 or len(k1h) < 30 or len(k4h) < 20:
                    continue

                sig = self.analyzer.analyze_mtf(
                    sym, k15, k1h, k4h,
                    min_score=min_score,
                    fee_mult=cfg.FEE_MULTIPLIER,
                    vol_mult=cfg.MIN_VOLUME_MULT,
                )
                if sig:
                    if sig.expected_pnl <= 0:
                        log.debug(f"[{sym}] PnL esperado negativo → HOLD")
                        continue
                    candidates.append(sig)
                    log.info(
                        f"🎯 SINAL PREMIUM: {sym} score={sig.score}/100 "
                        f"{sig.direction} R:R={sig.rr} "
                        f"PnL_líq≈+{sig.expected_pnl:.2f}% | {sig.reason}"
                    )
            except Exception as e:
                log.error(f"scan {sym}: {e}")
            await asyncio.sleep(0.6)   # respeita rate limit Bybit

        # Ordena por score decrescente e entra nos melhores
        candidates = self.analyzer.rank_signals(candidates)
        for sig in candidates:
            if len(self.positions) >= cfg.MAX_POSITIONS:
                break
            await self._open(sig)

    async def _open(self, sig: Signal):
        try:
            qty = self.risk.size(sig.symbol, sig.entry, self.instruments)
            if qty <= 0:
                log.warning(f"⚠️ {sig.symbol}: qty=0 — saldo insuficiente")
                return

            side = "Buy" if sig.direction == "LONG" else "Sell"
            await self.client.place_order(
                symbol=sig.symbol, side=side, qty=qty,
                sl=sig.sl, tp=sig.tp,
            )

            pos = Position(sig, qty)
            self.positions[sig.symbol] = pos
            log.info(
                f"✅ ABERTO {sig.direction} {qty} {sig.symbol} @ ${sig.entry:.4f} "
                f"SL=${sig.sl:.4f} TP=${sig.tp:.4f} Score={sig.score}/100 RR={sig.rr}"
            )
            await notify(await signal_msg(sig))
        except Exception as e:
            log.error(f"_open {sig.symbol}: {e}")

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
                if self.risk.drawdown >= cfg.MAX_DRAWDOWN:
                    log.warning(f"🚨 Drawdown {self.risk.drawdown:.1%} ≥ limite → pausando entradas")
                    self.active = False
                    await notify(f"⚠️ *Bot Pausado — Drawdown*\n`{self.risk.drawdown:.1%}` atingido")
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
