"""
NEXUS-7 Trading Engine v5.0
- Multi-símbolo (BTC, ETH, SOL, BNB, XRP)
- 100% automático
- Risk manager integrado
- Nunca derruba o servidor
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
        self.peak     = cfg.INITIAL_CAP
        self.balance  = cfg.INITIAL_CAP
        self.drawdown = 0.0
        self.losses   = 0
        self.wins     = 0
        self.total_pnl = 0.0

    def update(self, balance: float):
        if balance <= 0:
            return
        self.balance  = balance
        self.peak     = max(self.peak, balance)
        self.drawdown = (self.peak - balance) / self.peak

    def can_open(self, n_open: int) -> bool:
        if self.drawdown >= cfg.MAX_DRAWDOWN:
            log.warning(f"⚠️ Drawdown {self.drawdown:.1%} — bloqueando novas entradas")
            return False
        if n_open >= cfg.MAX_POSITIONS:
            return False
        return True

    def size(self, entry: float, sl: float) -> float:
        risk_usd = self.balance * cfg.MAX_RISK_PCT
        sl_dist  = abs(entry - sl)
        if sl_dist <= 0:
            return 0.001
        qty = risk_usd / sl_dist
        # Limita a 20% do saldo
        max_qty = (self.balance * 0.2) / entry
        qty = min(qty, max_qty)
        return max(0.001, round(qty, 3))

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

    # ── Lifecycle ─────────────────────────────────────────────
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
        log.info("⏸️ Bot pausado (servidor continua ativo)")

    # ── Connection ────────────────────────────────────────────
    async def _connect(self):
        try:
            ok = await self.client.ping()
            if not ok:
                log.error("❌ Bybit ping falhou")
                self.connected = False
                return

            bal = await self.client.get_balance()
            if bal < 0:
                log.error("❌ Auth Bybit falhou — verifique BYBIT_API_KEY e BYBIT_API_SECRET")
                self.connected = False
                return

            self.risk.balance = bal
            self.risk.peak    = max(self.risk.peak, bal)

            # Configura leverage em todos os símbolos
            for sym in cfg.SYMBOLS:
                await self.client.set_leverage(sym, cfg.LEVERAGE)
                await asyncio.sleep(0.3)

            self.connected = True
            self.active    = True
            log.info(f"✅ Bybit conectada! Saldo: ${bal:.2f} USDT | {len(cfg.SYMBOLS)} pares ativos")
            await notify(f"✅ *NEXUS-7 Online!*\nSaldo: `${bal:.2f} USDT`\nPares: `{', '.join(cfg.SYMBOLS)}`")

        except Exception as e:
            log.error(f"_connect: {e}")
            self.connected = False

    # ── Market scan ───────────────────────────────────────────
    async def _scan_markets(self):
        for symbol in cfg.SYMBOLS:
            if symbol in self.positions:
                continue  # já tem posição nesse par
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

    # ── Open position ─────────────────────────────────────────
    async def _open(self, sig: Signal):
        try:
            bal = await self.client.get_balance()
            if bal <= 0:
                return

            qty  = self.risk.size(sig.entry, sig.sl)
            side = "Buy" if sig.direction == "LONG" else "Sell"

            await self.client.place_order(
                symbol=sig.symbol, side=side, qty=qty,
                sl=sig.sl, tp=sig.tp
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

    # ── Monitor positions ─────────────────────────────────────
    async def _monitor_positions(self):
        if not self.positions:
            return
        try:
            all_pos = await self.client.get_positions()
            open_syms = {p["symbol"] for p in all_pos if float(p.get("size", 0)) > 0}

            for sym in list(self.positions.keys()):
                if sym not in open_syms:
                    pos = self.positions.pop(sym)
                    # Estima PnL
                    try:
                        ticker = await self.client.get_ticker(sym)
                        cur_price = float(ticker.get("lastPrice", pos.entry))
                        if pos.direction == "LONG":
                            pnl = (cur_price - pos.entry) * pos.qty
                        else:
                            pnl = (pos.entry - cur_price) * pos.qty
                    except Exception:
                        pnl = 0.0

                    self.risk.record(pnl)
                    icon = "✅" if pnl >= 0 else "❌"
                    log.info(f"📭 {sym} fechado | PnL: ${pnl:+.2f}")
                    await notify(f"{icon} *{sym} fechado*\nPnL: `${pnl:+.2f}`\nWin Rate: `{self.risk.win_rate:.0%}`")

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

    # ── Status ────────────────────────────────────────────────
    def get_status(self) -> dict:
        return {
            "connected":    self.connected,
            "active":       self.active,
            "balance":      round(self.risk.balance, 2),
            "drawdown_pct": round(self.risk.drawdown * 100, 2),
            "win_rate_pct": round(self.risk.win_rate * 100, 1),
            "wins":         self.risk.wins,
            "losses":       self.risk.losses,
            "total_pnl":    round(self.risk.total_pnl, 2),
            "positions":    [p.to_dict() for p in self.positions.values()],
            "symbols":      cfg.SYMBOLS,
            "recent_signals": self._signals_log[-5:],
        }
