import logging, sys, os
import json
import queue
import re
import threading
import time
import urllib.request


# ---------------------------------------------------------------------------
# NEXUS AI decision audit -> Telegram
# ---------------------------------------------------------------------------
# The engine already emits one canonical structured line before every new
# opening attempt:
#   [AI_DECISION] symbol=... side=... decision=... approved=... ... reason=...
#
# This handler mirrors those decisions to Telegram as PLAIN TEXT. It is kept
# independent from bot.notifier on purpose: notifier imports this logger, so
# importing notifier here would create a circular import. Plain text also
# avoids Telegram Markdown parse failures from hiding an AI decision.
#
# Delivery is non-blocking for the trading loop: logging only enqueues; a
# daemon worker performs HTTP I/O. Identical REJECT decisions are deduplicated
# for 15 minutes per symbol/reason. APPROVE events are always delivered.

_AI_TG_QUEUE = queue.Queue(maxsize=100)
_AI_TG_CACHE = {}
_AI_TG_LOCK = threading.Lock()
_AI_TG_WORKER_STARTED = False
_AI_TG_REJECT_COOLDOWN = int(os.environ.get("NEXUS_VETO_COOLDOWN", "900"))

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


def _ai_tg_enabled():
    return (
        os.environ.get("NEXUS_TELEGRAM", "true").lower() == "true"
        and bool(os.environ.get("TELEGRAM_TOKEN"))
        and bool(os.environ.get("TELEGRAM_CHAT"))
    )


def _ai_tg_format(d):
    approved = str(d.get("approved", "False")).lower() == "true"
    verdict = "✅ APPROVE" if approved else "🚫 REJECT"
    return (
        "🧠 NEXUS AI — DECISÃO\n"
        f"Par: {d.get('symbol', '?')}\n"
        f"Direção: {d.get('side', '?')}\n"
        f"Decisão: {verdict}\n"
        f"Score NEXUS: {d.get('score', 'N/A')}\n"
        f"Confiança: {d.get('confidence', 'N/A')}\n"
        f"Fonte: {d.get('source', 'N/A')}\n"
        f"Motivo: {d.get('reason', 'N/A')}\n"
        "Ordem: LIBERADA PARA PRÓXIMOS GATES" if approved else
        "🧠 NEXUS AI — DECISÃO\n"
        f"Par: {d.get('symbol', '?')}\n"
        f"Direção: {d.get('side', '?')}\n"
        f"Decisão: {verdict}\n"
        f"Score NEXUS: {d.get('score', 'N/A')}\n"
        f"Confiança: {d.get('confidence', 'N/A')}\n"
        f"Fonte: {d.get('source', 'N/A')}\n"
        f"Motivo: {d.get('reason', 'N/A')}\n"
        "Ordem: BLOQUEADA PELO GATE DA IA"
    )


def _ai_tg_should_send(d):
    approved = str(d.get("approved", "False")).lower() == "true"
    if approved:
        return True

    key = (d.get("symbol"), d.get("side"), d.get("reason"))
    now = time.time()
    with _AI_TG_LOCK:
        last = _AI_TG_CACHE.get(key, 0.0)
        if now - last < _AI_TG_REJECT_COOLDOWN:
            return False
        _AI_TG_CACHE[key] = now
        if len(_AI_TG_CACHE) > 500:
            cutoff = now - (_AI_TG_REJECT_COOLDOWN * 2)
            for k, t in list(_AI_TG_CACHE.items()):
                if t < cutoff:
                    _AI_TG_CACHE.pop(k, None)
    return True


def _ai_tg_worker():
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


def _ensure_ai_tg_worker():
    global _AI_TG_WORKER_STARTED
    if _AI_TG_WORKER_STARTED or not _ai_tg_enabled():
        return
    with _AI_TG_LOCK:
        if _AI_TG_WORKER_STARTED:
            return
        threading.Thread(
            target=_ai_tg_worker,
            name="nexus-ai-telegram-audit",
            daemon=True,
        ).start()
        _AI_TG_WORKER_STARTED = True


class _NexusDecisionTelegramHandler(logging.Handler):
    """Mirrors canonical [AI_DECISION] records to Telegram without blocking."""

    def emit(self, record):
        try:
            if not _ai_tg_enabled():
                return
            msg = record.getMessage()
            if not msg.startswith("[AI_DECISION]"):
                return
            m = _AI_DECISION_RE.match(msg)
            if not m:
                return
            data = m.groupdict()
            if not _ai_tg_should_send(data):
                return
            _ensure_ai_tg_worker()
            try:
                _AI_TG_QUEUE.put_nowait(_ai_tg_format(data))
            except queue.Full:
                pass
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
        lg.addHandler(_NexusDecisionTelegramHandler())
        lg.propagate = False
    return lg


log = _make("kakazito-trade")
