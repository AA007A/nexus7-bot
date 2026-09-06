import logging, sys, os
import json
import queue
import re
import threading
import time
import urllib.request
from collections import Counter


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
_OBS_FAILURE_LOCK = threading.Lock()
_OBS_FAILURE_LAST = {}
_OBS_FAILURE_COOLDOWN = 300.0
_AI_TG_REJECT_COOLDOWN = int(os.environ.get("NEXUS_VETO_COOLDOWN", "900"))
_FUNNEL_TG_COOLDOWN = int(os.environ.get("NEXUS_FUNNEL_COOLDOWN", "900"))

# Métricas operacionais desde o último startup. Não alteram decisões nem risco.
# Elas existem para responder objetivamente: quantos sinais chegaram à IA,
# quantos foram aprovados/vetados, por quê, e onde o funil pré-IA está parando.
_METRICS_LOCK = threading.Lock()
_AI_METRICS = {
    "started_at": time.time(),
    "total": 0,
    "approved": 0,
    "rejected": 0,
    "score_sum": 0.0,
    "score_count": 0,
    "reasons": Counter(),
    "sources": Counter(),
    "sides": Counter(),
    "funnel_total": 0,
    "funnel_stages": Counter(),
    "funnel_symbols": Counter(),
    "last_summary_ai_total": 0,
    "last_summary_funnel_total": 0,
}
_AI_METRICS_EVERY = max(1, int(os.environ.get("NEXUS_METRICS_EVERY", "10")))
_FUNNEL_METRICS_EVERY = max(10, int(os.environ.get("NEXUS_FUNNEL_METRICS_EVERY", "50")))

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


def _report_observability_failure(component, exc):
    """Report telemetry loss without re-entering logging or the trading loop."""
    try:
        now = time.monotonic()
        with _OBS_FAILURE_LOCK:
            last = _OBS_FAILURE_LAST.get(component, 0.0)
            if now - last < _OBS_FAILURE_COOLDOWN:
                return
            _OBS_FAILURE_LAST[component] = now
        sys.stderr.write(
            f"[OBSERVABILITY_FAILURE] component={component} "
            f"error={type(exc).__name__}\n"
        )
        sys.stderr.flush()
    except Exception:
        # Reporting telemetry loss must never affect order/risk execution.
        return


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def _metrics_snapshot_locked():
    total = _AI_METRICS["total"]
    approved = _AI_METRICS["approved"]
    rejected = _AI_METRICS["rejected"]
    avg_score = (
        _AI_METRICS["score_sum"] / _AI_METRICS["score_count"]
        if _AI_METRICS["score_count"] else None
    )
    return {
        "uptime_minutes": round((time.time() - _AI_METRICS["started_at"]) / 60.0, 1),
        "ai_total": total,
        "approved": approved,
        "rejected": rejected,
        "approval_rate_pct": round(approved / total * 100, 1) if total else 0.0,
        "avg_nexus_score": round(avg_score, 2) if avg_score is not None else None,
        "top_reject_reasons": _AI_METRICS["reasons"].most_common(5),
        "decision_sources": _AI_METRICS["sources"].most_common(5),
        "sides": _AI_METRICS["sides"].most_common(5),
        "pre_ai_total": _AI_METRICS["funnel_total"],
        "pre_ai_stages": _AI_METRICS["funnel_stages"].most_common(5),
        "pre_ai_symbols": _AI_METRICS["funnel_symbols"].most_common(5),
    }


def get_nexus_metrics():
    """Snapshot thread-safe das métricas NEXUS desde o último startup."""
    with _METRICS_LOCK:
        return _metrics_snapshot_locked()


def _metrics_summary_text(snapshot):
    reasons = ", ".join(f"{k}:{v}" for k, v in snapshot["top_reject_reasons"]) or "nenhum"
    stages = ", ".join(f"{k}:{v}" for k, v in snapshot["pre_ai_stages"]) or "nenhum"
    score = snapshot["avg_nexus_score"]
    return (
        "📊 NEXUS AI — MÉTRICAS\n"
        f"Decisões IA: {snapshot['ai_total']}\n"
        f"Aprovadas: {snapshot['approved']}\n"
        f"Vetadas: {snapshot['rejected']}\n"
        f"Taxa de aprovação: {snapshot['approval_rate_pct']:.1f}%\n"
        f"Score NEXUS médio: {score if score is not None else 'N/A'}\n"
        f"Principais vetos: {reasons}\n"
        f"Bloqueios pré-IA: {snapshot['pre_ai_total']}\n"
        f"Estágios pré-IA: {stages}\n"
        f"Janela: {snapshot['uptime_minutes']} min desde o startup"
    )


def _record_ai_metric(d):
    approved = str(d.get("approved", "False")).lower() == "true"
    with _METRICS_LOCK:
        _AI_METRICS["total"] += 1
        _AI_METRICS["approved" if approved else "rejected"] += 1
        _AI_METRICS["sources"][d.get("source", "unknown")] += 1
        _AI_METRICS["sides"][d.get("side", "unknown")] += 1
        if not approved:
            _AI_METRICS["reasons"][d.get("reason", "unknown")] += 1
        score = _safe_float(d.get("score"))
        if score is not None:
            _AI_METRICS["score_sum"] += score
            _AI_METRICS["score_count"] += 1
        due = (
            _AI_METRICS["total"] - _AI_METRICS["last_summary_ai_total"]
            >= _AI_METRICS_EVERY
        )
        if due:
            _AI_METRICS["last_summary_ai_total"] = _AI_METRICS["total"]
            snap = _metrics_snapshot_locked()
        else:
            snap = None
    if snap:
        _enqueue(_metrics_summary_text(snap))


def _record_funnel_metric(d):
    with _METRICS_LOCK:
        _AI_METRICS["funnel_total"] += 1
        _AI_METRICS["funnel_stages"][d.get("stage", "unknown")] += 1
        _AI_METRICS["funnel_symbols"][d.get("symbol", "unknown")] += 1
        due = (
            _AI_METRICS["funnel_total"] - _AI_METRICS["last_summary_funnel_total"]
            >= _FUNNEL_METRICS_EVERY
        )
        if due:
            _AI_METRICS["last_summary_funnel_total"] = _AI_METRICS["funnel_total"]
            snap = _metrics_snapshot_locked()
        else:
            snap = None
    if snap:
        _enqueue(_metrics_summary_text(snap))


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
        except Exception as exc:
            # Observability must never affect order/risk execution.
            _report_observability_failure("telegram_delivery", exc)
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
            msg = record.getMessage()

            if msg.startswith("[AI_DECISION]"):
                m = _AI_DECISION_RE.match(msg)
                if not m:
                    return
                data = m.groupdict()
                _record_ai_metric(data)
                if _tg_enabled() and _ai_tg_should_send(data):
                    _enqueue(_ai_tg_format(data))
                return

            data = _parse_funnel(msg)
            if data:
                _record_funnel_metric(data)
                if _tg_enabled() and _funnel_tg_should_send(data):
                    _enqueue(_funnel_tg_format(data))
        except Exception as exc:
            # Logging/Telegram/metrics can never break the engine.
            _report_observability_failure("decision_handler", exc)


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
