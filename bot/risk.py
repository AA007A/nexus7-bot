"""
BGX Capital — Risk Manager Unificado v2.0
Consolidado: este arquivo agora é a única fonte de verdade para risco.
O RiskManager em engine.py foi refatorado para usar os parâmetros de config.py.

Partial TPs: 50%/50% nos dois alvos técnicos
Trailing Stop: ATR-based, ativado após TRAILING_TRIGGER do alvo
Sizing: 1% do buying power por trade (MAX_RISK_PCT=0.01 × LEVERAGE=10 = 10% do saldo)
"""
import time, math
from dataclasses import dataclass, field
from bot.logger import log
from bot.config import cfg

# ── Constantes unificadas — lidas de config.py (NÃO hardcoded) ──
# REMOVIDOS: MAX_RISK_PCT=0.01 e MAX_DRAWDOWN=0.08 hardcoded
# Agora tudo vem de cfg para garantir consistência entre módulos

TP1_PCT = 0.50   # fecha 50% no primeiro alvo
TP2_PCT = 0.50   # fecha 50% no segundo alvo


@dataclass
class PositionRisk:
    symbol:      str
    direction:   str
    entry:       float
    sl:          float
    tp1:         float
    tp2:         float
    qty_total:   float
    qty_remain:  float = field(init=False)
    tp1_hit:     bool  = False
    tp2_hit:     bool  = False
    be_set:      bool  = False
    trailing_sl: float = 0.0
    opened_at:   float = field(default_factory=time.time)

    def __post_init__(self):
        self.qty_remain  = self.qty_total
        self.trailing_sl = self.sl

    def r_value(self) -> float:
        return abs(self.entry - self.sl)

    def to_dict(self) -> dict:
        return {
            "symbol":      self.symbol,
            "direction":   self.direction,
            "entry":       round(self.entry,       6),
            "sl":          round(self.trailing_sl, 6),
            "tp1":         round(self.tp1,         6),
            "tp2":         round(self.tp2,         6),
            "qty_total":   self.qty_total,
            "qty_remain":  round(self.qty_remain,  8),
            "tp1_hit":     self.tp1_hit,
            "tp2_hit":     self.tp2_hit,
            "be_set":      self.be_set,
            "trailing_sl": round(self.trailing_sl, 6),
        }


def build_position_risk(symbol: str, direction: str, entry: float,
                         sl: float, tp1: float, tp2: float, qty: float) -> PositionRisk:
    """Constrói PositionRisk com dois alvos técnicos reais (tp1 e tp2)."""
    return PositionRisk(
        symbol=symbol, direction=direction,
        entry=entry, sl=sl,
        tp1=tp1, tp2=tp2,
        qty_total=qty,
    )


def calc_position_size(balance: float, entry: float, sl: float,
                        leverage: int = None, size_mult: float = 1.0) -> float:
    """
    Calcula tamanho da posição baseado em risco fixo sobre o buying power.
    Fórmula: qty = (balance × leverage × MAX_RISK_PCT × size_mult) / entry
    Risco real sobre saldo = leverage × MAX_RISK_PCT (ex: 10 × 1% = 10% por trade)
    """
    if entry <= 0 or sl <= 0:
        return 0.0
    lev      = leverage or cfg.LEVERAGE
    notional = balance * lev * cfg.MAX_RISK_PCT * size_mult
    qty      = notional / entry
    return round(max(qty, 0.001), 6) if qty > 0 else 0.0


async def check_partial_tps(pos: PositionRisk, cur: float, client) -> dict:
    """
    Verifica e executa TPs parciais (50%/50%) com trailing stop após TP1.
    Após TP1: SL move para break-even.
    Após TP2: trailing stop dinâmico (0.5 × R abaixo do pico).
    """
    actions = []
    r = pos.r_value()
    if r <= 0:
        return {"actions": actions}

    # ── TP1 — fecha 50% ──────────────────────────────────────────
    if not pos.tp1_hit:
        hit = (
            (pos.direction == "LONG"  and cur >= pos.tp1) or
            (pos.direction == "SHORT" and cur <= pos.tp1)
        )
        if hit:
            q = round(pos.qty_total * TP1_PCT, 8)
            try:
                await client.place_order(
                    pos.symbol,
                    "Sell" if pos.direction == "LONG" else "Buy",
                    q,
                )
                pos.qty_remain  -= q
                pos.tp1_hit      = True
                pos.trailing_sl  = pos.entry   # move SL para break-even
                pos.be_set       = True
                await client.set_sl(pos.symbol, pos.entry)
                log.info(
                    f"✅ TP1 {pos.symbol}: {q:.6f} @ {cur:.4f} "
                    f"| BE={pos.entry:.4f} | remain={pos.qty_remain:.6f}"
                )
                actions.append("TP1")
            except Exception as e:
                log.error(f"TP1 {pos.symbol}: {e}")

    # ── TP2 — fecha os 50% restantes ─────────────────────────────
    elif not pos.tp2_hit:
        hit = (
            (pos.direction == "LONG"  and cur >= pos.tp2) or
            (pos.direction == "SHORT" and cur <= pos.tp2)
        )
        if hit:
            q = round(pos.qty_remain, 8)
            try:
                await client.place_order(
                    pos.symbol,
                    "Sell" if pos.direction == "LONG" else "Buy",
                    q,
                )
                pos.qty_remain -= q
                pos.tp2_hit     = True
                log.info(f"✅ TP2 {pos.symbol}: {q:.6f} @ {cur:.4f} | POSIÇÃO FECHADA")
                actions.append("TP2")
            except Exception as e:
                log.error(f"TP2 {pos.symbol}: {e}")

    # ── Trailing stop após TP1 ────────────────────────────────────
    if pos.tp1_hit and not pos.tp2_hit:
        new_sl = (
            cur - (r * 0.5) if pos.direction == "LONG"
            else cur + (r * 0.5)
        )
        better = (
            (pos.direction == "LONG"  and new_sl > pos.trailing_sl) or
            (pos.direction == "SHORT" and new_sl < pos.trailing_sl)
        )
        if better:
            pos.trailing_sl = new_sl
            try:
                await client.set_sl(pos.symbol, new_sl)
                log.info(f"🔄 Trailing SL {pos.symbol} → {new_sl:.4f}")
                actions.append("TRAIL_SL")
            except Exception as e:
                log.error(f"TrailSL {pos.symbol}: {e}")

    return {"actions": actions, "qty_remain": pos.qty_remain}


class RiskManager:
    """
    RiskManager UNIFICADO — única instância em todo o sistema.
    Parâmetros lidos exclusivamente de config.py.
    """
    def __init__(self):
        self.peak_balance = 0.0
        self.balance      = 0.0
        self.drawdown     = 0.0
        self._ready       = False
        self.positions: dict = {}

    def init(self, bal: float):
        if not self._ready and bal > 0:
            self.peak_balance = bal
            self.balance      = bal
            self._ready       = True
            log.info(
                f"📊 RiskManager: ${bal:.2f} | "
                f"poder=${bal * cfg.LEVERAGE:.2f} | "
                f"risco_trade={cfg.LEVERAGE * cfg.MAX_RISK_PCT * 100:.1f}% saldo"
            )

    def update(self, bal: float):
        if bal <= 0:
            return
        self.balance      = bal
        self.peak_balance = max(self.peak_balance, bal)
        self.drawdown     = (
            (self.peak_balance - bal) / self.peak_balance
            if self.peak_balance > 0 else 0.0
        )

    def can_open(self, n: int) -> bool:
        if not self._ready:
            return False
        if self.drawdown >= cfg.MAX_DRAWDOWN:
            log.warning(
                f"🚨 Drawdown {self.drawdown:.1%} ≥ limite "
                f"{cfg.MAX_DRAWDOWN:.0%} → bloqueado"
            )
            return False
        if n >= cfg.MAX_POSITIONS:
            log.info(f"⛔ {n}/{cfg.MAX_POSITIONS} posições → aguardando")
            return False
        return True

    def size(self, symbol: str, entry: float, instruments: dict,
             size_mult: float = 1.0) -> float:
        """
        Sizing com regra absoluta: margem nunca excede 80% do saldo.
        Risco por trade = balance × LEVERAGE × MAX_RISK_PCT
        """
        if entry <= 0 or not self._ready or self.balance <= 0:
            return 0.0

        info     = instruments.get(symbol, {})
        min_qty  = float(info.get("minQty",      0.001))
        qty_step = float(info.get("qtyStep",     0.001))
        min_not  = float(info.get("minNotional", 1.0))

        # Notional alvo: balance × leverage × MAX_RISK_PCT
        target_not   = self.balance * cfg.LEVERAGE * cfg.MAX_RISK_PCT * size_mult
        # Cap absoluto: nunca usar mais de 80% do saldo como margem
        max_notional = self.balance * 0.80 * cfg.LEVERAGE
        target_not   = min(target_not, max_notional)
        target_not   = max(target_not, min_not)

        qty   = target_not / entry
        steps = max(1, math.floor(qty / qty_step))
        qty   = round(steps * qty_step, 8)
        qty   = max(qty, min_qty)

        # Verificação hard: margem final nunca > 80% do saldo
        final_margin = (qty * entry) / cfg.LEVERAGE
        if final_margin > self.balance * 0.80:
            qty   = (self.balance * 0.80 * cfg.LEVERAGE) / entry
            steps = max(1, math.floor(qty / qty_step))
            qty   = round(steps * qty_step, 8)
            qty   = max(qty, min_qty)

        if qty * entry < min_not:
            log.warning(
                f"📐 {symbol}: saldo ${self.balance:.2f} insuficiente "
                f"(notional mínimo ${min_not})"
            )
            return 0.0

        log.info(
            f"📐 {symbol}: qty={qty} notional=${qty * entry:.2f} "
            f"margem=${qty * entry / cfg.LEVERAGE:.2f} / "
            f"saldo=${self.balance:.2f} "
            f"(risco={cfg.MAX_RISK_PCT * 100:.1f}% BP)"
        )
        return qty

    def open_position_risk(self, sig, qty: float) -> PositionRisk:
        tp1 = getattr(sig, "tp1", sig.tp)
        tp2 = getattr(sig, "tp2", sig.tp)
        pr  = build_position_risk(
            sig.symbol, sig.direction,
            sig.entry, sig.sl, tp1, tp2, qty
        )
        self.positions[sig.symbol] = pr
        return pr

    def close_position_risk(self, symbol: str):
        return self.positions.pop(symbol, None)
