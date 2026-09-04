import logging, sys, os
import json
import queue
import re
import threading
import time
import urllib.request


# ---------------------------------------------------------------------------
# Decision observability -> Telegram
# ---------------------------------------------------------------------------
# Mirrors the canonical NEXUS AI decision plus the pre-AI funnel to Telegram
# as PLAIN TEXT. This is intentionally independent from bot.notifier because
# notifier imports this logger; importing notifier here would create a cycle.
# Plain text also avoids Markdown parse failures hiding decision telemetry.
#
# Delivery never blocks the trading loop: logging only enqueues and a daemon
# worker performs HTTP I/O. Repeated HOLD/REJECT events are deduplicated.

_AI_TG_QUEUE = queue.Queue(maxsize=100)
_AI_TG_CACHE = {}
_AI_TG_LOCK = threading.Lock()
_AI_TG_WORKER_STARTED = False
_AI_TG_REJECT_COOLDOWN = int(os.environ.get("NEXUS_VETO_COOLDOWN", "900"))
_FUNNEL_TG_COOLDOWN = int(os.environ.get("NEXUS_FUNNEL_COOLDOWN", "900"))

_AI_DECISION_RE = re.compile(
    r"^\[AI_DECISION\]\s+"
    r"symbol=(?P<symbol>\S+)\s+"
    r"side=(?P<side>\S+)\s+"
    r"decision=(?P<decision>\S+)\s+"
    r"approved=(?P<approved>\S+)\s+"
    r"decision_source=(?P<source>\S+)\s+"
    r"score=(?P<score>\S+)\s+"
    r"confidence=(?P<confidence>\S+)\s+"
    r"ts=(?P<ts>\S+)\s+"
    r"reason=(?P<reason>.*)$"
)

_FUNNEL_SESSION_RE = re.compile(
    r"^⛔ \[(?P<symbol>[^\]]+)\] REJEITADO no ajuste de sessão: "
    r"(?P<score_before>[-+\d.]+)→(?P<score>[-+\d.]+) < (?P<minimum>[-+\d.]+) "
    r"\(sessão (?P<session>[^)]+)\)$"
)

_FUNNEL_REGIME_RE = re.compile(
    r"^⛔ \[(?P<symbol>[^\]]+)\] REJEITADO pelo regime: "
    r"(?P<side>\S+) não permitido em (?P<regime>\S+) "
    r"\(score era (?P<score>[-+\d.]+)\)$"
)

_FUNNEL_PNL_RE = re.compile(
    r"^⛔ \[(?P<symbol>[^\]]+)\] REJEITADO por PnL: "
    r"(?P<pnl>[-+\d.]+)% ≤ 0 após taxas "
    r"\(score (?P<score>[-+\d.]+), R:R (?P<rr>[-+\d.]+)\)$"
)

_FUNNEL_HOLD_RE = re.compile(
    r"^\[(?P<symbol>[^\]]+)\] Score=(?P<score>[-+\d.]+)/100 "
    r"\(4H:(?P<s4>[-+\d.]+) 1H:(?P<s1>[-+\d.]+) 15M:(?P<s15>[-+\d.]+)\) "
    r"\| regime=(?P<regime>\S+) RSI=(?P<rsi>[-+\d.]+) "
    r"vol=(?P<vol>[-+\d.]+)x \| 4H=(?P<t4>\S+) 1H=(?P<t1>\S+) → HOLD$"
)

_FUNNEL_DATA_RE = re.compile(
    r"^⛔ \[(?P<symbol>[^\]]+)\] SEM DADOS: "
    r"15m=(?P<d15>\d+)/(?P<m15>\d+) 1h=(?P<d1>\d+)/(?P<m1>\d+) "
    r"4h=(?P<d4>\d+)/(?P<m4>\d+) — cache e REST falharam$"
)


def _tg_enabled():
    return (
        os.environ.get("NEXUS_TELEGRAM", "true").lower() == "true"
        and bool(os.environ.get("TELEGRAM_TOKEN"))
        and bool(os.environ.get("TELEGRAM_CHAT"))
    )


def _ai_tg_format(d):
    approved = str(d.get("approved", "False")).lower() == "true"
    verdict = "✅ APPROVE" if approved else "🚫 REJECT"
    order_line = (
        "Ordem: LIBERADA PARA PRÓXIMOS GATES"
        if approved else
        "Ordem: BLOQUEADA PELO GATE DA IA"
    )
    return (
        "🧠 NEXUS AI — DECISÃO\n"
        f"Par: {d.get('symbol', '?')}\n"
        f"Direção: {d.get('side', '?')}\n"
        f"Decisão: {verdict}\n"
        f"Score NEXUS: {d.get('score', 'N/A')}\n"
        f"Confiança: {d.get('confidence', 'N/A')}\n"
        f"Fonte: {d.get('source', 'N/A')}\n"
        f"Motivo: {d.get('reason', 'N/A')}\n"
        f"{order_line}"
    )


def _parse_funnel(msg):
    m = _FUNNEL_SESSION_RE.match(msg)
    if m:
        d = m.groupdict()
        d.update(stage="SESSION", reason="score_reduzido_pela_sessao")
        return d

    m = _FUNNEL_REGIME_RE.match(msg)
    if m:
        d = m.groupdict()
        d.update(stage="REGIME", reason="direcao_incompativel_com_regime")
        return d

    m = _FUNNEL_PNL_RE.match(msg)
    if m:
        d = m.groupdict()
        d.update(stage="PNL_LIQUIDO", reason="pnl_esperado_nao_positivo")
        return d

    m = _FUNNEL_HOLD_RE.match(msg)
    if m:
        d = m.groupdict()
        d.update(stage="MTF", reason="setup_nao_atingiu_criterios_de_sinal")
        return d

    m = _FUNNEL_DATA_RE.match(msg)
    if m:
        d = m.groupdict()
        d.update(stage="DATA", reason="dados_insuficientes")
        return d

    return None


def _funnel_tg_format(d):
    stage = d.get("stage", "?")
    lines = [
        "🔎 NEXUS-7 — FUNIL PRÉ-IA",
        f"Par: {d.get('symbol', '?')}",
        f"Estágio: {stage}",
        "Decisão: ⏸ HOLD / NÃO ENVIAR À IA",
    ]

    if d.get("side"):
        lines.append(f"Direção: {d['side']}")
    if d.get("score") is not None:
        lines.append(f"Score: {d['score']}/100")
    if d.get("minimum") is not None:
        lines.append(f"Mínimo: {d['minimum']}/100")
    if d.get("regime"):
        lines.append(f"Regime: {d['regime']}")
    if d.get("session"):
        lines.append(f"Sessão: {d['session']}")
    if d.get("rr"):
        lines.append(f"R:R: {d['rr']}")
    if d.get("pnl"):
        lines.append(f"PnL esperado líquido: {d['pnl']}%")
    if d.get("s4"):
        lines.append(f"Timeframes: 4H={d['s4']} | 1H={d.get('s1')} | 15M={d.get('s15')}")
    if d.get("rsi"):
        lines.append(f"RSI 15M: {d['rsi']} | Volume: {d.get('vol')}x")
    if d.get("d15"):
        lines.append(
            f"Dados: 15M={d['d15']}/{d['m15']} | 1H={d['d1']}/{d['m1']} | 4H={d['d4']}/{d['m4']}"
        )

    lines.append(f"Motivo: {d.get('reason', 'N/A')}")
    lines.append("NEXUS AI: NÃO CHAMADA NESTE ESTÁGIO")
    return "\n".join(lines)


def _should_send(key, cooldown):
    now = time.time()
    with _AI_TG_LOCK:
        last = _AI_TG_CACHE.get(key, 0.0)
        if now - last < cooldown:
            return False
        _AI_TG_CACHE[key] = now
        if len(_AI_TG_CACHE) > 1000:
            cutoff = now - max(_AI_TG_REJECT_COOLDOWN, _FUNNEL_TG_COOLDOWN) * 2
            for k, t in list(_AI_TG_CACHE.items()):
                if t < cutoff:
                    _AI_TG_CACHE.pop(k, None)
    return True


def _ai_tg_should_send(d):
    approved = str(d.get("approved", "False")).lower() == "true"
    if approved:
        return True
    key = ("AI", d.get("symbol"), d.get("side"), d.get("reason"))
    return _should_send(key, _AI_TG_REJECT_COOLDOWN)


def _funnel_tg_should_send(d):
    key = (
        "FUNNEL",
        d.get("symbol"),
        d.get("stage"),
        d.get("reason"),
        d.get("regime"),
    )
    return _should_send(key, _FUNNEL_TG_COOLDOWN)


def _tg_worker():
    while True:
        text = _AI_TG_QUEUE.get()
        try:
            token = os.environ.get("TELEGRAM_TOKEN", "")
            chat = os.environ.get("TELEGRAM_CHAT", "")
            if not token or not chat:
                continue
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = json.dumps({"chat_id": chat, "text": text}).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                resp.read(64)
        except Exception:
            # Observability must never affect order/risk execution.
            pass
        finally:
            _AI_TG_QUEUE.task_done()


def _ensure_tg_worker():
    global _AI_TG_WORKER_STARTED
    if _AI_TG_WORKER_STARTED or not _tg_enabled():
        return
    with _AI_TG_LOCK:
        if _AI_TG_WORKER_STARTED:
            return
        threading.Thread(
            target=_tg_worker,
            name="nexus-decision-telegram-audit",
            daemon=True,
        ).start()
        _AI_TG_WORKER_STARTED = True


def _enqueue(text):
    _ensure_tg_worker()
    try:
        _AI_TG_QUEUE.put_nowait(text)
    except queue.Full:
        pass


class _DecisionTelegramHandler(logging.Handler):
    """Mirrors NEXUS AI and pre-AI funnel decisions without blocking."""

    def emit(self, record):
        try:
            if not _tg_enabled():
                return
            msg = record.getMessage()

            if msg.startswith("[AI_DECISION]"):
                m = _AI_DECISION_RE.match(msg)
                if not m:
                    return
                data = m.groupdict()
                if _ai_tg_should_send(data):
                    _enqueue(_ai_tg_format(data))
                return

            data = _parse_funnel(msg)
            if data and _funnel_tg_should_send(data):
                _enqueue(_funnel_tg_format(data))
        except Exception:
            # Logging/Telegram can never break the engine.
            pass


def _make(name):
    # .upper() garante que 'info'/'INFO'/'Info' todos viram 'INFO'
    # sem isso, getattr(logging, "info") retorna a funcao logging.info
    # em vez da constante logging.INFO, causando TypeError no setLevel
    level_str = os.environ.get("LOG_LEVEL", "INFO").upper()
    lvl = getattr(logging, level_str, logging.INFO)
    lg = logging.getLogger(name)
    if not lg.handlers:
        lg.setLevel(lvl)
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s",
            datefmt="%H:%M:%S"))
        lg.addHandler(h)
        lg.addHandler(_DecisionTelegramHandler())
        lg.propagate = False
    return lg


log = _make("kakazito-trade")
