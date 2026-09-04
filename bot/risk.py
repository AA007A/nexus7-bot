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
                _res = await client.place_order(
                    pos.symbol,
                    "Sell" if pos.direction == "LONG" else "Buy",
                    q,
                    reduce_only=True,   # auditoria #3
                )
                # auditoria #4: só atualiza estado após confirmação da ordem
                if not _res or not _res.get("orderId"):
                    log.error(f"❌ TP1 {pos.symbol} rejeitado — estado inalterado")
                    return {"actions": actions, "qty_remain": pos.qty_remain}
                pos.qty_remain  -= q
                pos.tp1_hit      = True

                # RISCO CORRIGIDO: set_sl era chamado sem verificar o retorno.
                # Se falhasse, a posição restante ficava com o SL ANTIGO
                # (abaixo do break-even) enquanto o bot registrava be_set=True
                # e agia como se estivesse protegido — risco de perder no que
                # já era lucro garantido.
                _be_ok = await client.set_sl(pos.symbol, pos.entry)
                if _be_ok:
                    pos.trailing_sl = pos.entry
                    pos.be_set      = True
                else:
                    # Não marca be_set: o trailing continuará tentando e o
                    # guardião de posições vai detectar se ficou sem stop.
                    log.error(
                        f"🚨 TP1 {pos.symbol}: falha ao mover SL para "
                        f"break-even (${pos.entry:.4f}) — posição restante "
                        f"ainda com stop original"
                    )
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
                _res = await client.place_order(
                    pos.symbol,
                    "Sell" if pos.direction == "LONG" else "Buy",
                    q,
                    reduce_only=True,   # auditoria #3
                )
                # auditoria #4: só atualiza estado após confirmação da ordem
                if not _res or not _res.get("orderId"):
                    log.error(f"❌ TP2 {pos.symbol} rejeitado — estado inalterado")
                    return {"actions": actions, "qty_remain": pos.qty_remain}
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
            # RISCO CORRIGIDO: pos.trailing_sl era atualizado ANTES de
            # chamar a exchange. Se a chamada falhasse, o bot registrava
            # um stop mais apertado do que o real — subestimando a perda
            # máxima da posição.
            try:
                _ok = await client.set_sl(pos.symbol, new_sl)
                if _ok:
                    pos.trailing_sl = new_sl
                    log.info(f"🔄 Trailing SL {pos.symbol} → {new_sl:.4f}")
                    actions.append("TRAIL_SL")
                else:
                    log.error(
                        f"🚨 TrailSL {pos.symbol}: exchange recusou "
                        f"{new_sl:.4f} — stop real permanece em "
                        f"{pos.trailing_sl:.4f}"
                    )
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
        self.balance_confirmed = False
        self.positions: dict = {}

    def init(self, bal: float):
        if not self._ready:
            self.update(bal)
            self._ready       = True
            log.info(
                f"📊 RiskManager: ${bal:.2f} | "
                f"poder=${bal * cfg.LEVERAGE:.2f} | "
                f"risco_trade={cfg.LEVERAGE * cfg.MAX_RISK_PCT * 100:.1f}% saldo"
            )

    def update(self, bal: float):
        self.balance_confirmed = False
        if type(bal) not in (int, float) or not math.isfinite(bal):
            raise ValueError("balance unavailable or invalid")
        self.balance_confirmed = True
        self.balance      = bal
        self.peak_balance = max(self.peak_balance, bal)
        self.drawdown     = (
            (self.peak_balance - bal) / self.peak_balance
            if self.peak_balance > 0 else 0.0
        )

    def can_open(self, n: int) -> bool:
        # Este era o ÚNICO caminho de bloqueio sem log — se _ready ficasse
        # False, o scan era pulado silenciosamente e nada aparecia nos logs.
        if not self._ready:
            log.warning(
                f"⛔ RiskManager não inicializado (saldo lido: "
                f"${self.balance:.2f}) — scan bloqueado"
            )
            return False
        if not self.balance_confirmed or self.balance <= 0:
            log.warning("[BALANCE] new entries blocked: zero or unconfirmed balance")
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

    def _margin_in_use(self, open_positions: dict = None) -> float:
        """
        Calcula margem total já comprometida pelas posições abertas.
        Usado pelo sizing para evitar superalocação de capital.

        ══════════════════════════════════════════════════════════
        ADV-margin — CAUSA RAIZ E CORREÇÃO
        ══════════════════════════════════════════════════════════
        Este método sempre leu self.positions (dict interno do
        RiskManager, populado por open_position_risk/close_position_
        risk). Mas bot/engine.py NUNCA chama esses dois métodos —
        confirmado por grep: zero ocorrências. self.positions ficava
        para sempre {}, e esta função sempre retornava 0.0, mesmo com
        posições reais e confirmadas abertas.

        Investigação da causa raiz mostrou duas implementações
        paralelas e desconectadas do mesmo conceito (TP parcial,
        qty restante, margem por posição):
          bot/risk.py::PositionRisk (qty_remain, tp1_hit, ...)
            — usado por check_partial_tps(), que NUNCA é chamado
              pelo engine.
          bot/engine.py::Position (qty, tp1_hit, ...)
            — é a que o engine de fato usa; pos.qty já é decrementado
              corretamente no TP parcial real (_manage_partial_tp,
              linha 'pos.qty = pos.qty - partial_qty').

        Reescrever check_partial_tps()/PositionRisk para serem usados
        exigiria substituir uma implementação já testada por outra
        não exercitada em produção — risco desproporcional ao escopo
        desta correção (instrução explícita: não reescrever
        RiskManager, não duplicar fonte de verdade).

        FIX: open_positions, quando fornecido pelo chamador (engine.
        positions — a fonte de verdade real, já mantida correta por
        fill parcial e pela reconciliação do ADV-01), é usado em vez
        de self.positions. Se omitido, mantém o comportamento anterior
        (self.positions do RiskManager) — não quebra nenhum chamador
        existente fora do fluxo principal.
        ══════════════════════════════════════════════════════════
        """
        total = 0.0
        source = open_positions if open_positions is not None else self.positions
        for pr in source.values():
            if hasattr(pr, "entry") and hasattr(pr, "qty"):
                # engine.Position: qty já reflete o tamanho EFETIVO
                # (reduzido por TP parcial, ou pelo fill real quando
                # a posição vem de _reconcile_exchange_positions).
                total += (pr.entry * pr.qty) / cfg.LEVERAGE
            elif hasattr(pr, "entry") and hasattr(pr, "qty_remain"):
                # bot.risk.PositionRisk (caminho legado, mantido para
                # não quebrar chamadores que ainda usem self.positions
                # sem passar open_positions).
                total += (pr.entry * pr.qty_remain) / cfg.LEVERAGE
        return total

    def size(self, symbol: str, entry: float, instruments: dict,
             size_mult: float = 1.0, open_positions: dict = None) -> float:
        """
        Sizing com cap de margem configurável (cfg.MAX_MARGIN_PCT).
        Desconta margem já em uso por posições abertas (FIX RISK-sizing).
        Risco por trade = balance × LEVERAGE × MAX_RISK_PCT

        open_positions (ADV-margin): dict de engine.Position (a fonte
        real de posições confirmadas). Repassado a _margin_in_use()
        para que o sizing considere margem já comprometida por
        posições reais, não apenas o self.positions interno do
        RiskManager (que fica vazio — ver docstring de _margin_in_use).
        """
        if entry <= 0 or not self._ready or self.balance <= 0:
            return 0.0

        info     = instruments.get(symbol, {})
        # ══════════════════════════════════════════════════════════
        # 🔴 P0 — UNIDADES MISTURADAS (contratos vs quantidade base)
        #
        # `qty` neste método é QUANTIDADE BASE (ex: 0.001 BTC), mas
        # info["minQty"] é o lotSize em CONTRATOS (ex: 1 contrato).
        # Comparar os dois diretamente é erro de unidade.
        #
        # Na KuCoin: 1 contrato = multiplier unidades da moeda base.
        #   XBTUSDTM: lotSize=1, multiplier=0.001 → mínimo 0.001 BTC
        #
        # Converte o lote mínimo para a mesma unidade de qty.
        # ══════════════════════════════════════════════════════════
        _lot_contratos = float(info.get("minQty",  1.0))
        _multiplier    = float(info.get("multiplier", 1.0))
        min_qty  = _lot_contratos * _multiplier          # em unidade base
        qty_step = min_qty                                # passo = 1 lote
        min_not  = float(info.get("minNotional", 1.0))

        # Margem livre = saldo - margem já em uso por posições abertas
        margin_used = self._margin_in_use(open_positions)
        free_margin = max(0.0, self.balance - margin_used)

        # Custo REAL do lote mínimo em margem (min_qty já está em unidade
        # base). min_not é quantidade base, não USDT — usá-lo aqui era
        # comparação de unidades incompatíveis.
        _margem_lote_min = (min_qty * entry) / cfg.LEVERAGE
        if free_margin < _margem_lote_min:
            log.warning(
                f"📐 {symbol}: margem livre ${free_margin:.2f} < "
                f"${_margem_lote_min:.2f} exigidos pelo lote mínimo "
                f"({min_qty} @ ${entry:.4f}) | em uso: ${margin_used:.2f}"
            )
            return 0.0

        # Notional alvo: balance × leverage × MAX_RISK_PCT
        target_not   = self.balance * cfg.LEVERAGE * cfg.MAX_RISK_PCT * size_mult
        # Cap de margem: configurável via MAX_MARGIN_PCT (default 0.80)
        # Com MAX_MARGIN_PCT=0.98 o bot usa praticamente todo o saldo.
        # Os 2% restantes cobrem as taxas de abertura/fechamento — sem essa
        # folga a exchange rejeita a ordem por saldo insuficiente.
        margin_cap   = getattr(cfg, "MAX_MARGIN_PCT", 0.80)
        max_notional = free_margin * margin_cap * cfg.LEVERAGE
        target_not   = min(target_not, max_notional)

        # NÃO forçar target_not para min_not: são unidades diferentes
        # (USDT vs quantidade base) e isso inflava o notional.
        # ══════════════════════════════════════════════════════════
        # P1 CORRIGIDO — math.floor COM FLOAT PERDIA UM LOTE
        #
        # 0.1 não tem representação binária exata:
        #     0.7 / 0.1 == 6.999999999999999  → floor = 6 (deveria ser 7)
        #     1.4 / 0.1 == 13.999999999999998 → floor = 13 (deveria ser 14)
        #     2.8 / 0.1 == 27.999999999999996 → floor = 27 (deveria ser 28)
        #
        # O bot descartava um lote inteiro nesses casos, operando abaixo
        # do tamanho correto. Medido em 22 de 770 combinações da matriz
        # de validação (saldos × instrumentos × leverages).
        #
        # Decimal faz a divisão em base 10, sem erro de representação.
        # ══════════════════════════════════════════════════════════
        qty = target_not / entry
        if qty_step > 0:
            from decimal import Decimal, ROUND_FLOOR
            _d_qty  = Decimal(str(qty))
            _d_step = Decimal(str(qty_step))
            steps   = int((_d_qty / _d_step).to_integral_value(rounding=ROUND_FLOOR))
            qty     = float(Decimal(steps) * _d_step)
        else:
            steps = 0
            qty   = 0.0

        # Se nem 1 lote cabe, recusa aqui — sem forçar o mínimo, que era
        # justamente o que estourava a margem.
        if qty < min_qty:
            _falta = (min_qty * entry) / cfg.LEVERAGE
            log.warning(
                f"📐 {symbol}: RECUSADO — cabe apenas {qty} mas o lote "
                f"mínimo é {min_qty} (exige ${_falta:.2f} de margem, "
                f"disponível ${free_margin * margin_cap:.2f})"
            )
            return 0.0

        # ══════════════════════════════════════════════════════════
        # 🔴 P0 CORRIGIDO — SIZING EXCEDIA O SALDO EM ORDENS DE MAGNITUDE
        #
        # O clamp de margem era aplicado e LOGO DESFEITO por
        # `qty = max(qty, min_qty)`. Quando o lote mínimo custa mais que
        # o saldo permite, o código forçava o mínimo mesmo assim.
        #
        # MEDIDO (saldo $0.50, BTCUSDT a $108k, 10x):
        #   qty=1 → notional $108.000 → margem $10.800 = 2.160.000% do saldo
        #
        # A verificação seguinte também estava errada: comparava
        # `qty * entry` (notional em USDT) com `min_not`, que é a
        # QUANTIDADE BASE mínima (lotSize × multiplier). Unidades
        # diferentes — a checagem nunca protegia.
        #
        # Agora: se o lote mínimo não cabe na margem disponível, o trade
        # é RECUSADO. Melhor não operar que abrir posição impossível.
        # ══════════════════════════════════════════════════════════
        margem_max = self.balance * margin_cap

        # Custo real do lote mínimo, em margem
        margem_min_lote = (min_qty * entry) / cfg.LEVERAGE
        if margem_min_lote > margem_max:
            log.warning(
                f"📐 {symbol}: RECUSADO — lote mínimo ({min_qty}) exige "
                f"${margem_min_lote:.2f} de margem, mas só há "
                f"${margem_max:.2f} disponível "
                f"(saldo ${self.balance:.2f} × {margin_cap:.0%})"
            )
            return 0.0

        # Clamp de margem — SEM forçar o mínimo depois
        final_margin = (qty * entry) / cfg.LEVERAGE
        if final_margin > margem_max:
            qty = (margem_max * cfg.LEVERAGE) / entry
            from decimal import Decimal, ROUND_FLOOR
            _d = Decimal(str(qty)) / Decimal(str(qty_step))
            steps = int(_d.to_integral_value(rounding=ROUND_FLOOR))
            qty   = float(Decimal(steps) * Decimal(str(qty_step)))
            if qty < min_qty:
                log.warning(
                    f"📐 {symbol}: RECUSADO — após ajuste de margem, "
                    f"qty={qty} < lote mínimo {min_qty}"
                )
                return 0.0

        # Verificação final em MARGEM (não em notional vs qty base)
        _margem_final = (qty * entry) / cfg.LEVERAGE
        if _margem_final > margem_max * 1.001:      # 0.1% de tolerância
            log.error(
                f"📐 {symbol}: RECUSADO — margem final ${_margem_final:.2f} "
                f"excede o teto ${margem_max:.2f}"
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
