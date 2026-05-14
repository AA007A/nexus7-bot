"""
NEXUS-7 Trading Engine v5.2
- Opera com qualquer saldo acima de $0.10
- Multi-símbolo automático
"""
import asyncio
from datetime import datetime
from typing import Dict, Optional

from bot.bybit import BybitClient
from bot.strategy import Analyzer, Signal
from bot.config import cfg
from bot.logger import log
from bot.notifier import notify, signal_msg


class Position:
    def __init__(self, sig: Signal, qty: float):
        self.symbol    = sig.symbol
        self.direction = sig.direction
        self.entry     = sig.entry
        self.sl        = sig.sl
        self.tp        = sig.tp
        self.qty       = qty
        self.opened_at = datetime.utcnow()
        self.pnl       = 0.0

    def to_dict(self):
        return {
            "symbol":    self.symbol,
            "direction": self.direction,
            "entry":     round(self.entry, 2),
            "sl":        round(self.sl, 2),
            "tp":        round(self.tp, 2),
            "qty":       self.qty,
            "pnl":       round(self.pnl, 2),
            "opened_at": str(self.opened_at),
        }


class RiskManager:
    def __init__(self):
        self.peak      = 0.0
        self.balance   = 0.0
        self.drawdown  = 0.0
        self.losses    = 0
        self.wins      = 0
        self.total_pnl = 0.0
        self._initialized = False

    def init(self, balance: float):
        if not self._initialized and balance > 0:
            self.peak         = balance
            self.balance      = balance
            self._initialized = True
            log.info(f"📊 Risk Manager iniciado com ${balance:.4f}")

    def update(self, balance: float):
        if balance <= 0:
            return
        self.balance  = balance
        self.peak     = max(self.peak, balance)
        if self.peak > 0:
            self.drawdown = (self.peak - balance) / self.peak

    def can_open(self, n_open: int) -> bool:
        if not self._initialized:
            return False
        # Mínimo $0.10 para operar
        if self.balance < 0.10:
            log.warning(f"⚠️ Saldo insuficiente: ${self.balance:.4f} USDT")
            return False
        if self.drawdown >= cfg.MAX_DRAWDOWN:
            log.warning(f"⚠️ Drawdown {self.drawdown:.1%} — bloqueando")
            return False
        if n_open >= cfg.MAX_POSITIONS:
            return False
        return True

    def size(self, symbol: str, entry: float, sl: float) -> float:
        """
        Calcula qty respeitando:
        1. Poder de compra real (balance * leverage)
        2. Qty mínima da Bybit por símbolo
        3. Notional mínimo da Bybit ($5)
        4. Notional máximo = 80% do poder de compra
        """
        from bot.strategy import MIN_QTY, MIN_NOTIONAL
        if entry <= 0:
            return 0.0

        buying_power = self.balance * cfg.LEVERAGE
        min_qty      = MIN_QTY.get(symbol, 0.001)
        min_notional = MIN_NOTIONAL.get(symbol, 2.0)

        # Sem poder de compra suficiente
        if buying_power < min_notional:
            log.warning(f"⚠️ ${buying_power:.2f} insuficiente para {symbol} (mín ${min_notional})")
            return 0.0

        # Usa 80% do poder de compra
        target_notional = buying_power * 0.8
        qty = target_notional / entry
        qty = max(qty, min_qty)

        # Nunca ultrapassa o poder de compra
        if qty * entry > buying_power:
            qty = (buying_power * 0.95) / entry
            qty = max(qty, min_qty)

        qty = round(qty, 3)
        notional = qty * entry
        log.info(f"📐 {symbol}: qty={qty} notional=${notional:.2f} | power=${buying_power:.2f}")
        return qty

    def record(self, pnl: float):
        self.total_pnl += pnl
        if pnl >= 0:
            self.wins += 1
        else:
            self.losses += 1

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0.0


class TradingEngine:
    def __init__(self, client: BybitClient):
        self.client    = client
        self.analyzer  = Analyzer()
        self.risk      = RiskManager()
        self.positions: Dict[str, Position] = {}
        self.connected = False
        self.active    = False
        self._running  = False
        self._signals_log = []

    async def run(self):
        if self._running:
            return
        self._running = True
        log.info("⚡ Engine iniciando...")
        await self._connect()

        while self._running:
            try:
                if not self.connected:
                    await asyncio.sleep(30)
                    await self._connect()
                    continue

                if self.active:
                    await self._update_balance()
                    if self.risk.can_open(len(self.positions)):
                        await self._scan_markets()
                    await self._monitor_positions()

                await asyncio.sleep(15)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Engine: {e}")
                await asyncio.sleep(10)

    def stop(self):
        self.active = False
        log.info("⏸️ Bot pausado")

    async def _connect(self):
        try:
            ok = await self.client.ping()
            if not ok:
                log.error("❌ Bybit ping falhou")
                self.connected = False
                return

            bal = await self.client.get_balance()
            if bal < 0:
                log.error("❌ Auth falhou — verifique BYBIT_API_KEY e BYBIT_API_SECRET")
                self.connected = False
                return

            self.risk.init(bal)
            self.risk.update(bal)

            # Configura leverage em todos os pares
            for sym in cfg.SYMBOLS:
                await self.client.set_leverage(sym, cfg.LEVERAGE)
                await asyncio.sleep(0.3)

            # Carrega posições já abertas na Bybit
            await self._load_existing_positions()

            self.connected = True
            self.active    = True
            log.info(f"✅ Bybit conectada! Saldo: ${bal:.4f} USDT | Leverage: {cfg.LEVERAGE}x")
            log.info(f"💪 Poder de compra: ${bal * cfg.LEVERAGE:.2f} USDT")
            await notify(
                f"✅ *NEXUS-7 Online!*\n"
                f"Saldo: `${bal:.4f} USDT`\n"
                f"Leverage: `{cfg.LEVERAGE}x`\n"
                f"Poder de compra: `${bal * cfg.LEVERAGE:.2f}`\n"
                f"Pares: `{', '.join(cfg.SYMBOLS)}`"
            )

        except Exception as e:
            log.error(f"_connect: {e}")
            self.connected = False

    async def _scan_markets(self):
        """Varre todos os pares em busca de sinais"""
        for symbol in cfg.SYMBOLS:
            if symbol in self.positions:
                continue
            try:
                klines = await self.client.get_klines(symbol, "15m", 100)
                if len(klines) < 50:
                    continue
                sig = self.analyzer.analyze(symbol, klines)
                if sig and sig.confidence >= cfg.MIN_CONFIDENCE:
                    log.info(f"📊 [{symbol}] {sig.direction} conf={sig.confidence:.0%} | {sig.reason}")
                    await self._open(sig)
                    await asyncio.sleep(1)
            except Exception as e:
                log.error(f"scan {symbol}: {e}")
            await asyncio.sleep(0.5)

    async def _open(self, sig: Signal):
        try:
            bal = await self.client.get_balance()
            if bal < 0.10:
                log.warning(f"⚠️ Saldo muito baixo para operar: ${bal:.4f}")
                return

            qty  = self.risk.size(sig.symbol, sig.entry, sig.sl)
            if qty <= 0:
                log.warning(f"⚠️ Qty 0 para {sig.symbol} — saldo insuficiente")
                return

            side = "Buy" if sig.direction == "LONG" else "Sell"

            await self.client.place_order(
                symbol=sig.symbol,
                side=side,
                qty=qty,
                sl=sig.sl,
                tp=sig.tp,
            )

            pos = Position(sig, qty)
            self.positions[sig.symbol] = pos

            msg = await signal_msg(sig)
            await notify(msg)

            self._signals_log.append({
                "symbol":    sig.symbol,
                "direction": sig.direction,
                "entry":     sig.entry,
                "conf":      sig.confidence,
                "reason":    sig.reason,
                "time":      str(datetime.utcnow()),
            })
            if len(self._signals_log) > 50:
                self._signals_log.pop(0)

            log.info(f"✅ {sig.direction} {qty} {sig.symbol} @ ${sig.entry:.2f}")

        except Exception as e:
            log.error(f"_open {sig.symbol}: {e}")

    async def _monitor_positions(self):
        if not self.positions:
            return
        try:
            all_pos   = await self.client.get_positions()
            open_syms = {p["symbol"] for p in all_pos if float(p.get("size", 0)) > 0}

            for sym in list(self.positions.keys()):
                if sym not in open_syms:
                    pos = self.positions.pop(sym)
                    try:
                        ticker    = await self.client.get_ticker(sym)
                        cur_price = float(ticker.get("lastPrice", pos.entry))
                        pnl = (cur_price - pos.entry) * pos.qty if pos.direction == "LONG" \
                              else (pos.entry - cur_price) * pos.qty
                    except Exception:
                        pnl = 0.0

                    self.risk.record(pnl)
                    icon = "✅" if pnl >= 0 else "❌"
                    log.info(f"📭 {sym} fechado | PnL: ${pnl:+.4f}")
                    await notify(
                        f"{icon} *{sym} fechado*\n"
                        f"PnL: `${pnl:+.4f}`\n"
                        f"Win Rate: `{self.risk.win_rate:.0%}`"
                    )
        except Exception as e:
            log.error(f"_monitor: {e}")

    async def _update_balance(self):
        try:
            bal = await self.client.get_balance()
            if bal >= 0:
                self.risk.update(bal)
                if self.risk.drawdown >= cfg.MAX_DRAWDOWN:
                    log.warning(f"🚨 Drawdown {self.risk.drawdown:.1%} — pausando bot")
                    self.active = False
                    await notify(f"⚠️ *NEXUS-7 Pausado*\nDrawdown: `{self.risk.drawdown:.1%}`")
        except Exception:
            pass

    async def _load_existing_positions(self):
        """Carrega posições já abertas na Bybit ao iniciar"""
        try:
            all_pos = await self.client.get_positions()
            loaded = 0
            for p in all_pos:
                size = float(p.get("size", 0))
                if size <= 0:
                    continue
                sym  = p.get("symbol", "")
                side = p.get("side", "")
                if not sym or not side:
                    continue
                direction = "LONG" if side == "Buy" else "SHORT"
                entry = float(p.get("avgPrice", 0))
                liq   = float(p.get("liqPrice", 0))
                # Estima SL e TP baseado na posição
                atr_est = entry * 0.007
                if direction == "LONG":
                    sl = max(liq * 1.01, entry - atr_est * 1.2)
                    tp = entry + atr_est * 2.5
                else:
                    sl = min(liq * 0.99, entry + atr_est * 1.2)
                    tp = entry - atr_est * 2.5

                # Cria objeto Signal mínimo para Position
                from bot.strategy import Signal
                sig = Signal(sym, direction, entry, sl, tp, 0.75, "Posição existente carregada")
                pos = Position(sig, size)
                pos.pnl = float(p.get("unrealisedPnl", 0))
                self.positions[sym] = pos
                loaded += 1
                log.info(f"📂 Posição carregada: {direction} {size} {sym} @ ${entry:.2f} | PnL: ${pos.pnl:.4f}")

            if loaded > 0:
                log.info(f"✅ {loaded} posição(ões) existente(s) carregada(s)")
            else:
                log.info("📭 Nenhuma posição existente encontrada")
        except Exception as e:
            log.error(f"_load_existing_positions: {e}")

    def get_status(self) -> dict:
        return {
            "connected":      self.connected,
            "active":         self.active,
            "balance":        round(self.risk.balance, 4),
            "buying_power":   round(self.risk.balance * cfg.LEVERAGE, 2),
            "drawdown_pct":   round(self.risk.drawdown * 100, 2),
            "win_rate_pct":   round(self.risk.win_rate * 100, 1),
            "wins":           self.risk.wins,
            "losses":         self.risk.losses,
            "total_pnl":      round(self.risk.total_pnl, 4),
            "positions":      [p.to_dict() for p in self.positions.values()],
            "symbols":        cfg.SYMBOLS,
            "leverage":       cfg.LEVERAGE,
            "recent_signals": self._signals_log[-5:],
        }
# patch applied below in _connect
