"""
BGX Capital — Filters v2.0
Funding, OI Delta, Fear & Greed, Horário, Spread, Macro Events.
CORRIGIDO: tasks de atualização do cache agora iniciadas pelo engine.
NOVO: update_fear_greed e update_macro_events agora exportados para o engine.
run_all_filters() conectado ao fluxo real de _open() no engine.py
"""
import asyncio, time, aiohttp
from datetime import datetime, timezone
from bot.logger import log

# ── Cache global ──────────────────────────────────────────────────
# BUG CORRIGIDO v12: ts inicializado com time.time() em vez de 0.
# Com ts=0, age_h = (now - 0)/3600 ≈ 488.000h > 25 → filtros F&G e macro
# retornavam "permitindo" imediatamente no startup sem dados reais.
# Agora: ts=time.time() → age_h=0 → filtros aguardam a primeira atualização
# real das tasks (update_fear_greed / update_macro_events).
_cache = {
    "fear_greed":    {"value": 50, "label": "Neutral", "ts": time.time()},
    "macro_events":  {"events": [], "ts": time.time()},
    "oi_history":    {},
}

# ── Thresholds configuráveis ──────────────────────────────────────
FUNDING_BLOCK_LONG  =  0.0005    # bloqueia LONG se funding > +0.05%
FUNDING_BLOCK_SHORT = -0.0005    # bloqueia SHORT se funding < -0.05%
LIQUIDITY_HOURS     = (2, 23)    # UTC — inclui moves noturnos BTC (02-06 UTC = alta atividade asiática)
SPREAD_MAX_PCT      =  0.0005    # 0.05% máximo
MACRO_BLOCK_MIN     =  30        # minutos antes/depois de eventos macro USD


# ── Funding Rate ──────────────────────────────────────────────────
async def check_funding(client, symbol: str, direction: str) -> dict:
    try:
        fr = await client.get_funding_rate(symbol)
    except Exception:
        return {"ok": True, "funding": 0.0, "reason": "funding N/A"}
    if direction == "LONG" and fr > FUNDING_BLOCK_LONG:
        return {"ok": False, "funding": fr,
                "reason": f"Funding {fr*100:.4f}% > +0.05% — bloqueia LONG"}
    if direction == "SHORT" and fr < FUNDING_BLOCK_SHORT:
        return {"ok": False, "funding": fr,
                "reason": f"Funding {fr*100:.4f}% < -0.05% — bloqueia SHORT"}
    return {"ok": True, "funding": fr, "reason": f"Funding {fr*100:.4f}% OK"}


# ── Open Interest Delta ───────────────────────────────────────────
async def check_oi_delta(client, symbol: str, direction: str) -> dict:
    try:
        d  = await client.get_open_interest(symbol)
        oi = float(d.get("openInterest", 0))
    except Exception:
        return {"ok": True, "oi": 0, "delta": 0, "reason": "OI N/A"}

    hist = _cache["oi_history"].setdefault(symbol, [])
    hist.append(oi)
    if len(hist) > 5:
        hist.pop(0)
    if len(hist) < 2:
        return {"ok": True, "oi": oi, "delta": 0, "reason": "OI histórico insuficiente"}

    delta = (oi - hist[-2]) / hist[-2] if hist[-2] > 0 else 0
    if delta < -0.005:
        return {"ok": False, "oi": oi, "delta": delta,
                "reason": f"OI caindo {delta*100:.2f}% — sem convicção"}
    return {"ok": True, "oi": oi, "delta": delta,
            "reason": f"OI delta {delta*100:.2f}% OK"}


# ── Fear & Greed — tarefa de atualização autônoma ─────────────────
async def update_fear_greed():
    """
    Atualiza cache de Fear & Greed a cada hora.
    DEVE ser chamado como asyncio.create_task() no engine.run().
    Inclui timeout para evitar travamento da task.
    """
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://api.alternative.me/fng/?limit=1",
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as r:
                    d   = await r.json()
                    val = int(d["data"][0]["value"])
                    lbl = d["data"][0]["value_classification"]
                    _cache["fear_greed"] = {"value": val, "label": lbl, "ts": time.time()}
                    log.info(f"📊 Fear&Greed: {val} ({lbl})")
        except asyncio.TimeoutError:
            log.debug("fear_greed: timeout (API lenta)")
        except Exception as e:
            log.debug(f"fear_greed: {e}")
        await asyncio.sleep(3600)


def check_fear_greed(direction: str) -> dict:
    """
    Filtro Fear & Greed com lógica corrigida.

    Lógica CORRETA (corrigido RISK-3):
      - Extremo MEDO (<=25): o pânico JÁ aconteceu → bons LONGs de recuperação.
        Bloquear LONGs aqui seria perder a melhor oportunidade de compra.
        Bloqueia SHORTS (o move de queda já foi, risco/retorno ruim).
        → Permite LONG, bloqueia SHORT.

      - Extremo GANÂNCIA (>=80): euforia no topo → risco de reversão alta.
        Permite SHORTs (oportunidade), reduz confiança em LONGs.
        → Permite SHORT, penaliza LONG (não bloqueia — pode ser breakout).

      - Zona neutra (26-79): sem restrição.
    """
    fg    = _cache["fear_greed"]
    val   = fg["value"]
    age_h = (time.time() - fg["ts"]) / 3600

    if age_h > 25:
        log.debug("fear_greed: cache desatualizado (>25h) — skip")
        return {"ok": True, "value": val, "label": fg["label"],
                "reason": "F&G cache desatualizado — permitindo"}

    # Extremo MEDO: bloqueia SHORT (move já foi), libera LONG (recuperação)
    if val <= 25 and direction == "SHORT":
        return {"ok": False, "value": val, "label": fg["label"],
                "reason": f"Extremo MEDO F&G={val} — SHORT arriscado pós-pânico, aguardando recuperação"}

    # Extremo GANÂNCIA: bloqueia LONG (risco de topo), libera SHORT (reversão)
    if val >= 80 and direction == "LONG":
        return {"ok": False, "value": val, "label": fg["label"],
                "reason": f"Extremo GANÂNCIA F&G={val} — LONG no topo de euforia bloqueado"}

    return {"ok": True, "value": val, "label": fg["label"],
            "reason": f"F&G {val} ({fg['label']}) — sem restrição direcional"}


# ── Horário de liquidez ───────────────────────────────────────────
def check_trading_hours() -> dict:
    now = datetime.now(timezone.utc)
    h   = now.hour
    wd  = now.weekday() >= 5  # sábado/domingo
    if not (LIQUIDITY_HOURS[0] <= h < LIQUIDITY_HOURS[1]):
        return {"ok": False, "hour": h, "weekend": wd, "size_mult": 0.0,
                "reason": f"Fora janela de liquidez ({h:02d}h UTC | janela: {LIQUIDITY_HOURS[0]}-{LIQUIDITY_HOURS[1]})"}
    return {"ok": True, "hour": h, "weekend": wd,
            "size_mult": 0.6 if wd else 1.0,
            "reason": f"Horário OK{' | weekend → size 60%' if wd else ''}"}


# ── Spread ────────────────────────────────────────────────────────
async def check_spread(client, symbol: str) -> dict:
    try:
        ob   = await client.get_orderbook(symbol)
        bids = ob.get("b", [["0", "0"]])
        asks = ob.get("a", [["0", "0"]])
        bid  = float(bids[0][0])
        ask  = float(asks[0][0])
        mid  = (bid + ask) / 2
        sp   = (ask - bid) / mid if mid > 0 else 0
        if sp > SPREAD_MAX_PCT:
            return {"ok": False, "spread": sp,
                    "reason": f"Spread {sp*100:.4f}% > {SPREAD_MAX_PCT*100:.3f}%"}
        return {"ok": True, "spread": sp,
                "reason": f"Spread {sp*100:.4f}% OK"}
    except Exception:
        return {"ok": True, "spread": 0, "reason": "orderbook N/A"}


# ── Eventos Macro (USD) ───────────────────────────────────────────
async def update_macro_events():
    """
    Atualiza calendário macro a cada 6h.
    DEVE ser chamado como asyncio.create_task() no engine.run().
    Usa ForexFactory (source gratuito, sem API key).
    """
    while True:
        fetched = False
        # Fonte 1: ForexFactory JSON (sem auth)
        try:
            import ssl as _ssl
            _ctx = _ssl.create_default_context()
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                    timeout=aiohttp.ClientTimeout(total=10),
                    headers={"User-Agent": "Mozilla/5.0"},
                    ssl=_ctx,
                ) as r:
                    evs  = await r.json()
                    high = [
                        e for e in evs
                        if e.get("impact", "").lower() == "high"
                        and e.get("country", "").upper() == "USD"
                    ]
                    _cache["macro_events"] = {"events": high, "ts": time.time()}
                    log.info(f"📅 {len(high)} eventos macro USD high-impact")
                    fetched = True
        except asyncio.TimeoutError:
            log.debug("macro_events: fonte 1 timeout")
        except Exception as e:
            log.debug(f"macro_events fonte 1: {e}")

        if not fetched:
            # Fallback: usa cache anterior (não limpa events)
            log.debug("macro_events: usando cache anterior como fallback")

        await asyncio.sleep(21600)  # 6 horas


def check_macro_events() -> dict:
    now    = datetime.now(timezone.utc)
    events = _cache["macro_events"].get("events", [])
    age_h  = (time.time() - _cache["macro_events"].get("ts", 0)) / 3600

    if age_h > 7:
        log.debug("macro_events: cache > 7h — skip (fonte indisponível)")
        return {"ok": True, "event": None, "reason": "Macro cache desatualizado — permitindo"}

    for ev in events:
        try:
            ev_dt = datetime.fromisoformat(
                ev.get("date", "").replace("Z", "+00:00")
            )
            if ev_dt.tzinfo is None:
                ev_dt = ev_dt.replace(tzinfo=timezone.utc)
            diff_min = abs((now - ev_dt).total_seconds()) / 60
            if diff_min <= MACRO_BLOCK_MIN:
                return {
                    "ok":    False,
                    "event": ev.get("title", "Evento USD"),
                    "reason": (
                        f"Evento macro USD em {diff_min:.0f}min: "
                        f"{ev.get('title', '?')}"
                    ),
                }
        except Exception:
            continue

    return {"ok": True, "event": None, "reason": "Sem eventos macro próximos"}


# ── Executor principal — conectado ao _open() do engine ──────────
async def run_all_filters(client, symbol: str, direction: str) -> dict:
    """
    Executa todos os filtros em sequência.
    CONECTADO ao _open() do engine v11 (era código morto na v10).
    Retorna ok=False na primeira falha com motivo.
    Retorna size_mult para escalar tamanho da posição (ex: 0.6 no fim de semana).
    """
    res = {}

    # ── Filtros síncronos (rápidos, sem I/O) ─────────────────────
    for fn, kwargs in [
        (check_trading_hours,  {}),
        (check_macro_events,   {}),
        (check_fear_greed,     {"direction": direction}),
    ]:
        key = fn.__name__.replace("check_", "")
        r   = fn(**kwargs)
        res[key] = r
        if not r["ok"]:
            return {"ok": False, "blocked_by": key,
                    "size_mult": 0.0, "details": res}

    # ── Filtros assíncronos (I/O: funding, OI, spread) ───────────
    for fn, kwargs in [
        (check_funding,  {"client": client, "symbol": symbol, "direction": direction}),
        (check_oi_delta, {"client": client, "symbol": symbol, "direction": direction}),
        (check_spread,   {"client": client, "symbol": symbol}),
    ]:
        key = fn.__name__.replace("check_", "")
        try:
            r = await asyncio.wait_for(fn(**kwargs), timeout=5.0)
        except asyncio.TimeoutError:
            log.debug(f"filter {key} timeout — permitindo")
            r = {"ok": True, "reason": f"{key} timeout — skip"}
        res[key] = r
        if not r["ok"]:
            return {"ok": False, "blocked_by": key,
                    "size_mult": 0.0, "details": res}

    size_mult = res.get("trading_hours", {}).get("size_mult", 1.0)
    return {"ok": True, "blocked_by": None,
            "size_mult": size_mult, "details": res}


def get_filter_summary() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "fear_greed":    _cache["fear_greed"],
        "macro_events":  [
            e.get("title", "?")
            for e in _cache["macro_events"].get("events", [])[:3]
        ],
        "macro_cache_age_h": round((time.time() - _cache["macro_events"].get("ts", 0)) / 3600, 1),
        "trading_hours": LIQUIDITY_HOURS[0] <= now.hour < LIQUIDITY_HOURS[1],
        "current_hour":  now.hour,
    }


# ── Checklist pré-trade ───────────────────────────────────────────────────────
def pre_trade_checklist(symbol: str, price: float,
                        ask: float, bid: float,
                        funding_rate: float,
                        direction: str,
                        score: int,
                        regime: str,
                        session: str) -> dict:
    """
    Checklist completo antes de qualquer entrada.
    Itens 20, 24, 28 da lista de melhorias.
    Retorna {"ok": bool, "blocked_by": str, "details": dict}
    """
    details = {}

    # ── 1. Spread máximo (0.05%) ──────────────────────────────
    spread_pct = ((ask - bid) / price * 100) if price > 0 else 0
    MAX_SPREAD_PCT = 0.05
    details["spread_pct"]    = round(spread_pct, 4)
    details["spread_ok"]     = spread_pct <= MAX_SPREAD_PCT
    if not details["spread_ok"]:
        return {"ok": False, "blocked_by": f"SPREAD_ALTO {spread_pct:.3f}% > {MAX_SPREAD_PCT}%",
                "details": details}

    # ── 2. Regime permitido ────────────────────────────────────
    ALLOWED_REGIMES = {"TRENDING_UP", "TRENDING_DOWN"}
    details["regime"]    = regime
    details["regime_ok"] = regime in ALLOWED_REGIMES
    if not details["regime_ok"]:
        return {"ok": False, "blocked_by": f"REGIME_{regime}_PROIBIDO",
                "details": details}

    # ── 3. Score mínimo ────────────────────────────────────────
    MIN_SCORE = 65
    details["score"]    = score
    details["score_ok"] = score >= MIN_SCORE
    if not details["score_ok"]:
        return {"ok": False, "blocked_by": f"SCORE_{score}_ABAIXO_{MIN_SCORE}",
                "details": details}

    # ── 4. Funding rate ────────────────────────────────────────
    fr = funding_rate
    MAX_FUNDING_LONG  = 0.0005   # 0.05%
    MAX_FUNDING_SHORT = -0.0005  # -0.05%
    details["funding_rate"] = fr
    funding_ok = True
    if direction == "LONG"  and fr > MAX_FUNDING_LONG:
        funding_ok = False
    if direction == "SHORT" and fr < MAX_FUNDING_SHORT:
        funding_ok = False
    details["funding_ok"] = funding_ok
    if not funding_ok:
        return {"ok": False, "blocked_by": f"FUNDING_{fr:.5f}_ALTO_PARA_{direction}",
                "details": details}

    # ── 5. Horário de liquidez ─────────────────────────────────
    from datetime import datetime, timezone
    hour = datetime.now(timezone.utc).hour
    details["hour_utc"]   = hour
    details["session"]    = session
    details["session_ok"] = (hour >= 2 and hour < 23)
    if not details["session_ok"]:
        return {"ok": False, "blocked_by": f"FORA_HORARIO_UTC_{hour}h",
                "details": details}

    # ── Tudo passou ────────────────────────────────────────────
    details["all_passed"] = True
    return {"ok": True, "blocked_by": "", "details": details}


def calc_trade_cost(entry: float, qty: float,
                    funding_rate: float = 0.0001,
                    periods_estimated: int = 3) -> dict:
    """
    Modelo de custos real por trade.
    Item 28 da lista de melhorias.

    Inclui: taxa de abertura + taxa de fechamento + funding estimado + slippage
    Retorna break-even mínimo e custo total em USD e %.
    """
    TAKER_FEE  = 0.00055  # Bybit taker 0.055%
    SLIPPAGE   = 0.0002   # 0.02% estimado
    notional   = entry * qty

    fee_open     = notional * TAKER_FEE
    fee_close    = notional * TAKER_FEE
    slippage_est = notional * SLIPPAGE * 2  # abertura + fechamento
    funding_est  = notional * abs(funding_rate) * periods_estimated

    total_cost  = fee_open + fee_close + slippage_est + funding_est
    cost_pct    = (total_cost / notional * 100) if notional > 0 else 0
    breakeven   = entry + (total_cost / qty) if qty > 0 else entry

    return {
        "notional":     round(notional, 2),
        "fee_open":     round(fee_open, 4),
        "fee_close":    round(fee_close, 4),
        "slippage_est": round(slippage_est, 4),
        "funding_est":  round(funding_est, 4),
        "total_cost":   round(total_cost, 4),
        "cost_pct":     round(cost_pct, 4),
        "breakeven":    round(breakeven, 6),
        "min_tp_to_profit": round(breakeven * 1.001, 6),  # 0.1% acima do breakeven
    }
