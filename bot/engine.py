"""
NEXUS-7 Trading Engine v6.0
- Auto-detecta mínimos reais da Bybit
- Trailing stop quando lucro >= 50%
- Até 8 posições simultâneas
- PnL tracking: sessão, 1d, 7d, 30d
- Posições abertas sempre visíveis
"""
import asyncio, time
from datetime import datetime, timedelta
from typing import Dict, Optional, List

from bot.bybit import BybitClient
from bot.strategy import Analyzer, Signal
from bot.config import cfg
from bot.logger import log
from bot.notifier import notify, signal_msg


class Trade:
    """Histórico de um trade fechado"""
    def __init__(self, symbol, direction, entry, exit_price, qty, pnl, opened_at):
        self.symbol      = symbol
        self.direction   = direction
        self.entry       = entry
        self.exit_price  = exit_price
        self.qty         = qty
        self.pnl         = pnl
        self.opened_at   = opened_at
        self.closed_at   = datetime.utcnow()


class Position:
    def __init__(self, sig: Signal, qty: float):
        self.symbol      = sig.symbol
        self.direction   = sig.direction
        self.entry       = sig.entry
        self.sl          = sig.sl
        self.tp          = sig.tp
        self.qty         = qty
        self.opened_at   = datetime.utcnow()
        self.pnl         = 0.0
        self.peak_pnl    = 0.0   # maior lucro já atingido
        self.trailing_sl = None  # stop loss do trailing
        self.trailing_active = False

    def update_pnl(self, current_price: float):
        if self.direction == "LONG":
            self.pnl = (current_price - self.entry) * self.qty
        else:
            self.pnl = (self.entry - current_price) * self.qty
        if self.pnl > self.peak_pnl:
            self.peak_pnl = self.pnl

    def pnl_pct(self) -> float:
        """Percentual de lucro em relação ao entry"""
        if self.entry <= 0:
            return 0.0
        if self.direction == "LONG":
            return (self.pnl / (self.entry * self.qty)) if self.qty > 0 else 0.0
        return (self.pnl / (self.entry * self.qty)) if self.qty > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "symbol":          self.symbol,
            "direction":       self.direction,
            "entry":           round(self.entry, 6),
            "sl":              round(self.trailing_sl or self.sl, 6),
            "tp":              round(self.tp, 6),
            "qty":             self.qty,
            "pnl":             round(self.pnl, 4),
            "pnl_pct":         round(self.pnl_pct() * 100, 2),
            "peak_pnl":        round(self.peak_pnl, 4),
            "trailing_active": self.trailing_active,
            "opened_at":       str(self.opened_at),
        }


class Stats:
    """PnL por período"""
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
            return {"pnl": 0.0, "wins": 0, "losses": 0, "win_rate": 0.0, "trades": 0}
        wins   = [t for t in trades if t.pnl >= 0]
        losses = [t for t in trades if t.pnl < 0]
        return {
            "pnl":      round(sum(t.pnl for t in trades), 4),
            "wins":     len(wins),
            "losses":   len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1),
            "trades":   len(trades),
        }

    def all_summaries(self) -> dict:
        return {
            "session": self.summary(),
            "1d":      self.summary(1),
            "7d":      self.summary(7),
            "30d":     self.summary(30),
        }


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
            return False
        if n_open >= cfg.MAX_POSITIONS:
            return False
        return True

    def size(self, symbol: str, entry: float, instruments: dict) -> float:
        """Calcula qty usando info real da Bybit"""
        if entry <= 0 or not self._ready:
            return 0.0
        info = instruments.get(symbol, {})
        min_qty  = info.get("minQty",  0.001)
        qty_step = info.get("qtyStep", 0.001)
        min_not  = info.get("minNotional", 1.0)

        buying_power  = self.balance * cfg.LEVERAGE
        target_not    = buying_power * cfg.MAX_RISK_PCT
        target_not    = max(target_not, min_not)

        # Não pode usar mais que 95% do poder de compra
        if target_not > buying_power * 0.95:
            target_not = buying_power * 0.95

        qty = target_not / entry

        # Arredonda para o step correto
        steps = int(qty / qty_step)
        qty   = round(steps * qty_step, 8)
        qty   = max(qty, min_qty)

        notional = qty * entry
        if notional > buying_power:
            qty = (buying_power * 0.90) / entry
            steps = int(qty / qty_step)
            qty   = round(steps * qty_step, 8)
            qty   = max(qty, min_qty)

        notional = qty * entry
        log.info(f"📐 {symbol}: qty={qty} notional=${notional:.3f} step={qty_step}")
        return qty


class TradingEngine:
    def __init__(self, client: BybitClient):
        self.client      = client
        self.analyzer    = Analyzer()
        self.risk        = RiskManager()
        self.stats       = Stats()
        self.positions:  Dict[str, Position] = {}
        self.instruments: dict = {}
        self.viable_symbols: List[str] = []
        self.connected   = False
        self.active      = False
        self._running    = False
        self._scan_idx   = 0   # rotação dos símbolos

    # ── Lifecycle ─────────────────────────────────────────────
    async def run(self):
        if self._running:
            return
        self._running = True
        log.info("⚡ Engine v6.0 iniciando...")
        await self._connect()

        while self._running:
            try:
                if not self.connected:
                    await asyncio.sleep(30)
                    await self._connect()
                    continue

                if self.active:
                    await self._update_balance()
                    await self._update_open_positions()
                    if self.risk.can_open(len(self.positions)):
                        await self._scan_next()

                await asyncio.sleep(15)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Engine: {e}")
                await asyncio.sleep(10)

    def stop(self):
        self.active = False
        log.info("⏸️ Bot pausado")

    # ── Connect ───────────────────────────────────────────────
    async def _connect(self):
        try:
            if not await self.client.ping():
                log.error("❌ Bybit ping falhou")
                self.connected = False
                return

            bal = await self.client.get_balance()
            if bal < 0:
                log.error("❌ Auth falhou")
                self.connected = False
                return

            self.risk.init(bal)
            self.risk.update(bal)

            # Carrega instrumentos reais
            self.instruments = await self.client.get_instruments()

            # Filtra símbolos viáveis com o saldo atual
            await self._filter_viable_symbols()

            # Configura leverage
            for sym in self.viable_symbols[:10]:
                await self.client.set_leverage(sym, cfg.LEVERAGE)
                await asyncio.sleep(0.2)

            # Carrega posições existentes
            await self._load_existing()

            self.connected = True
            self.active    = True
            log.info(f"✅ Conectado! ${bal:.4f} USDT | {len(self.viable_symbols)} pares viáveis")
            await notify(
                f"✅ *NEXUS-7 v6 Online!*\n"
                f"Saldo: `${bal:.4f} USDT`\n"
                f"Poder: `${bal*cfg.LEVERAGE:.2f} USDT`\n"
                f"Pares: `{len(self.viable_symbols)}`\n"
                f"Max posições: `{cfg.MAX_POSITIONS}`"
            )
        except Exception as e:
            log.error(f"_connect: {e}")
            self.connected = False

    async def _filter_viable_symbols(self):
        """Filtra símbolos onde o saldo permite abrir posição mínima"""
        viable = []
        try:
            tickers = await self.client.get_all_tickers()
            price_map = {t["symbol"]: float(t.get("lastPrice", 0)) for t in tickers}

            buying_power = self.risk.balance * cfg.LEVERAGE

            for sym in cfg.SYMBOLS:
                info = self.instruments.get(sym)
                if not info:
                    continue
                price    = price_map.get(sym, 0)
                if price <= 0:
                    continue
                min_not  = info.get("minNotional", 1.0)
                min_qty  = info.get("minQty", 0.001)
                min_cost = max(min_not, min_qty * price)

                if buying_power >= min_cost * 1.1:
                    viable.append(sym)

            self.viable_symbols = viable
            log.info(f"✅ {len(viable)} pares viáveis: {viable[:8]}...")
        except Exception as e:
            log.error(f"_filter_viable: {e}")
            self.viable_symbols = cfg.SYMBOLS[:5]

    # ── Scan (rotação) ────────────────────────────────────────
    async def _scan_next(self):
        """Escaneia 3 símbolos por ciclo em rotação"""
        if not self.viable_symbols:
            return
        for _ in range(3):
            sym = self.viable_symbols[self._scan_idx % len(self.viable_symbols)]
            self._scan_idx += 1
            if sym in self.positions:
                continue
            try:
                klines = await self.client.get_klines(sym, "15m", 100)
                if len(klines) < 50:
                    continue
                sig = self.analyzer.analyze(sym, klines)
                if sig and sig.confidence >= cfg.MIN_CONFIDENCE:
                    log.info(f"📊 [{sym}] {sig.direction} {sig.confidence:.0%} | {sig.reason}")
                    await self._open(sig)
            except Exception as e:
                log.error(f"scan {sym}: {e}")
            await asyncio.sleep(0.5)

    # ── Open ──────────────────────────────────────────────────
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
            log.info(f"✅ {sig.direction} {qty} {sig.symbol} @ ${sig.entry:.4f}")

            await notify(await signal_msg(sig))
        except Exception as e:
            log.error(f"_open {sig.symbol}: {e}")

    # ── Update open positions + trailing stop ─────────────────
    async def _update_open_positions(self):
        if not self.positions:
            return
        try:
            all_pos   = await self.client.get_positions()
            open_syms = {}
            for p in all_pos:
                size = float(p.get("size", 0))
                if size > 0:
                    open_syms[p["symbol"]] = p

            for sym in list(self.positions.keys()):
                pos = self.positions[sym]

                if sym not in open_syms:
                    # Posição fechada
                    pnl  = pos.pnl
                    icon = "✅" if pnl >= 0 else "❌"
                    trade = Trade(sym, pos.direction, pos.entry,
                                  pos.entry + (pnl/pos.qty if pos.qty else 0),
                                  pos.qty, pnl, pos.opened_at)
                    self.stats.add(trade)
                    self.positions.pop(sym)
                    log.info(f"📭 {sym} fechado PnL=${pnl:+.4f}")
                    await notify(
                        f"{icon} *{sym} fechado*\n"
                        f"Direção: `{pos.direction}`\n"
                        f"PnL: `${pnl:+.4f}`\n"
                        f"📊 Sessão: `${self.stats.summary()['pnl']:+.4f}`"
                    )
                else:
                    # Atualiza PnL e trailing stop
                    bybit_pos = open_syms[sym]
                    upnl = float(bybit_pos.get("unrealisedPnl", 0))
                    cur  = float(bybit_pos.get("markPrice", pos.entry))
                    pos.update_pnl(cur)
                    pos.pnl = upnl

                    # Trailing stop — ativa quando lucro >= 50%
                    await self._check_trailing(pos, cur)

        except Exception as e:
            log.error(f"_update_positions: {e}")

    async def _check_trailing(self, pos: Position, current_price: float):
        """Ativa trailing stop quando lucro >= TRAILING_TRIGGER (50%)"""
        if pos.qty <= 0 or pos.entry <= 0:
            return

        # Calcula % de lucro
        if pos.direction == "LONG":
            pnl_pct = (current_price - pos.entry) / pos.entry
        else:
            pnl_pct = (pos.entry - current_price) / pos.entry

        # Ativa trailing quando >= 50% de lucro
        if pnl_pct >= cfg.TRAILING_TRIGGER and not pos.trailing_active:
            pos.trailing_active = True
            log.info(f"🔄 {pos.symbol} trailing ativado! Lucro={pnl_pct:.0%}")
            await notify(
                f"🔄 *Trailing Stop Ativado!*\n"
                f"Par: `{pos.symbol}`\n"
                f"Lucro: `{pnl_pct:.0%}`\n"
                f"Garantindo 25% do pico"
            )

        if pos.trailing_active:
            # Trava 25% do pico de lucro
            if pos.direction == "LONG":
                peak_price = pos.entry * (1 + pos.pnl / (pos.entry * pos.qty))
                new_sl = pos.entry + (peak_price - pos.entry) * cfg.TRAILING_LOCK
                if pos.trailing_sl is None or new_sl > pos.trailing_sl:
                    pos.trailing_sl = new_sl
                    log.info(f"🔄 {pos.symbol} trailing SL → ${new_sl:.4f}")
            else:
                peak_price = pos.entry * (1 - pos.pnl / (pos.entry * pos.qty))
                new_sl = pos.entry - (pos.entry - peak_price) * cfg.TRAILING_LOCK
                if pos.trailing_sl is None or new_sl < pos.trailing_sl:
                    pos.trailing_sl = new_sl
                    log.info(f"🔄 {pos.symbol} trailing SL → ${new_sl:.4f}")

    # ── Load existing positions ───────────────────────────────
    async def _load_existing(self):
        try:
            all_pos = await self.client.get_positions()
            count   = 0
            for p in all_pos:
                size = float(p.get("size", 0))
                if size <= 0:
                    continue
                sym  = p["symbol"]
                side = p.get("side", "Buy")
                direction = "LONG" if side == "Buy" else "SHORT"
                entry = float(p.get("avgPrice", 0))
                upnl  = float(p.get("unrealisedPnl", 0))
                liq   = float(p.get("liqPrice", 0))

                atr_est = entry * 0.007
                if direction == "LONG":
                    sl = max(liq * 1.02, entry - atr_est * 1.5) if liq > 0 else entry - atr_est * 1.5
                    tp = entry + atr_est * 3.0
                else:
                    sl = min(liq * 0.98, entry + atr_est * 1.5) if liq > 0 else entry + atr_est * 1.5
                    tp = entry - atr_est * 3.0

                from bot.strategy import Signal
                sig = Signal(sym, direction, entry, sl, tp, 0.75, "Carregada do Bybit")
                pos = Position(sig, size)
                pos.pnl = upnl
                self.positions[sym] = pos
                count += 1
                log.info(f"📂 Carregada: {direction} {size} {sym} @ ${entry:.4f} PnL=${upnl:.4f}")

            if count:
                log.info(f"✅ {count} posição(ões) carregada(s)")
        except Exception as e:
            log.error(f"_load_existing: {e}")

    async def _update_balance(self):
        try:
            bal = await self.client.get_balance()
            if bal >= 0:
                self.risk.update(bal)
                if self.risk.drawdown >= cfg.MAX_DRAWDOWN:
                    log.warning(f"🚨 Drawdown {self.risk.drawdown:.1%} — pausando")
                    self.active = False
                    await notify(f"⚠️ *Bot Pausado*\nDrawdown: `{self.risk.drawdown:.1%}`")
        except Exception:
            pass

    # ── Status ────────────────────────────────────────────────
    def get_status(self) -> dict:
        summaries = self.stats.all_summaries()
        return {
            "connected":       self.connected,
            "active":          self.active,
            "balance":         round(self.risk.balance, 4),
            "buying_power":    round(self.risk.balance * cfg.LEVERAGE, 2),
            "drawdown_pct":    round(self.risk.drawdown * 100, 2),
            "leverage":        cfg.LEVERAGE,
            "max_positions":   cfg.MAX_POSITIONS,
            "viable_symbols":  len(self.viable_symbols),
            "positions":       [p.to_dict() for p in self.positions.values()],
            "pnl_session":     summaries["session"],
            "pnl_1d":          summaries["1d"],
            "pnl_7d":          summaries["7d"],
            "pnl_30d":         summaries["30d"],
            # legacy fields para dashboard
            "wins":            summaries["session"]["wins"],
            "losses":          summaries["session"]["losses"],
            "win_rate_pct":    summaries["session"]["win_rate"],
            "total_pnl":       summaries["session"]["pnl"],
            "symbols":         self.viable_symbols[:10],
        }
