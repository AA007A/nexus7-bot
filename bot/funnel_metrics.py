"""Read-only runtime instrumentation for the pre-NEXUS decision funnel.

Counts where market evaluations are rejected before reaching NEXUS AI. This
module never changes a score, signal, risk limit, trading mode, or order path.
It observes canonical log messages emitted by strategy/engine and exposes a
thread-safe snapshot for the Control Center.
"""
from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter

_LOCK = threading.Lock()
_INSTALLED = False
_STARTED_AT = time.time()
_BLOCKS = Counter()
_SYMBOLS = Counter()
_CANDIDATES = 0
_AI_REACHED = 0
_AI_APPROVED = 0
_AI_REJECTED = 0
_LAST_EVENT = None
_LAST_SUMMARY_BLOCKS = 0

_SUMMARY_EVERY = max(25, int(os.environ.get("FUNNEL_METRICS_EVERY", "100")))
_TELEGRAM_ENABLED = os.environ.get("FUNNEL_TELEGRAM", "true").lower() == "true"
_TG_QUEUE: queue.Queue[str] = queue.Queue(maxsize=20)
_TG_STARTED = False

# Ordered: first matching terminal pre-AI gate wins.
_RULES = [
    ("DATA", re.compile(r"^⛔ \[(?P<symbol>[^\]]+)\] (?:CANDLES INSUFICIENTES|SEM DADOS):")),
    ("REGIME_4H", re.compile(r"^⛔ \[(?P<symbol>[^\]]+)\] REGIME \S+ no 4H")),
    ("MTF_4H_1H", re.compile(r"^⛔ \[(?P<symbol>[^\]]+)\] 4H/1H NÃO ALINHADOS")),
    ("SCORE", re.compile(r"^\[(?P<symbol>[^\]]+)\] Score=[-+\d.]+/100 < [-+\d.]+ → HOLD")),
    ("RSI", re.compile(r"^⛔ \[(?P<symbol>[^\]]+)\] BLOQUEIO RSI extremo:")),
    ("VOLUME", re.compile(r"^⛔ \[(?P<symbol>[^\]]+)\] BLOQUEIO volume:")),
    ("ALIGN_15M", re.compile(r"^⛔ \[(?P<symbol>[^\]]+)\] BLOQUEIO alinhamento:")),
    ("ENTRY_TRIGGER", re.compile(r"^⛔ \[(?P<symbol>[^\]]+)\] BLOQUEIO setup:")),
    ("RR", re.compile(r"^\[(?P<symbol>[^\]]+)\] R:R [-+\d.]+ < [-+\d.]+ → HOLD")),
    ("COSTS", re.compile(r"^\[(?P<symbol>[^\]]+)\] Move [-+\d.]+% < [-+\d.]+% → HOLD")),
    ("SESSION", re.compile(r"^⛔ \[(?P<symbol>[^\]]+)\] REJEITADO no ajuste de sessão:")),
    ("REGIME_POST", re.compile(r"^⛔ \[(?P<symbol>[^\]]+)\] REJEITADO pelo regime:")),
    ("PNL_LIQUIDO", re.compile(r"^⛔ \[(?P<symbol>[^\]]+)\] REJEITADO por PnL:")),
]
_CANDIDATE_RE = re.compile(r"^✅ \[(?P<symbol>[^\]]+)\] CANDIDATO:")
_AI_RE = re.compile(
    r"^\[AI_DECISION\].*?symbol=(?P<symbol>\S+).*?approved=(?P<approved>\S+)"
)


def _snapshot_locked() -> dict:
    blocked = sum(_BLOCKS.values())
    denom = blocked if blocked else 1
    ordered = _BLOCKS.most_common()
    return {
        "started_at": _STARTED_AT,
        "uptime_minutes": round((time.time() - _STARTED_AT) / 60.0, 1),
        "blocked_total": blocked,
        "blocks": dict(ordered),
        "block_percentages": {
            k: round(v / denom * 100.0, 1) for k, v in ordered
        },
        "top_symbols": _SYMBOLS.most_common(12),
        "candidates": _CANDIDATES,
        "ai_reached": _AI_REACHED,
        "ai_approved": _AI_APPROVED,
        "ai_rejected": _AI_REJECTED,
        "last_event": dict(_LAST_EVENT) if _LAST_EVENT else None,
        "note": "Contadores observacionais desde o último startup; regras de trading não foram alteradas.",
    }


def get_funnel_metrics() -> dict:
    with _LOCK:
        return _snapshot_locked()


def _summary_text(s: dict) -> str:
    blocks = list(s.get("blocks", {}).items())[:8]
    pct = s.get("block_percentages", {})
    detail = "\n".join(f"• {k}: {v} ({pct.get(k, 0):.1f}%)" for k, v in blocks) or "• nenhum"
    return (
        "📊 NEXUS-7 — FUNIL MEDIDO\n"
        f"Bloqueios pré-IA: {s['blocked_total']}\n"
        f"Candidatos: {s['candidates']}\n"
        f"Chegaram à NEXUS AI: {s['ai_reached']}\n"
        f"IA aprovou: {s['ai_approved']} | vetou: {s['ai_rejected']}\n"
        "Principais gargalos:\n"
        f"{detail}\n"
        f"Janela: {s['uptime_minutes']} min desde o startup"
    )


def _tg_worker() -> None:
    while True:
        text = _TG_QUEUE.get()
        try:
            token = os.environ.get("TELEGRAM_TOKEN", "")
            chat = os.environ.get("TELEGRAM_CHAT", "")
            if not token or not chat:
                continue
            data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=data,
                method="POST",
            )
            urllib.request.urlopen(req, timeout=8).read()
        except Exception:
            pass
        finally:
            _TG_QUEUE.task_done()


def _enqueue_summary(text: str) -> None:
    global _TG_STARTED
    if not _TELEGRAM_ENABLED:
        return
    if not os.environ.get("TELEGRAM_TOKEN") or not os.environ.get("TELEGRAM_CHAT"):
        return
    if not _TG_STARTED:
        _TG_STARTED = True
        threading.Thread(target=_tg_worker, name="funnel-telegram", daemon=True).start()
    try:
        _TG_QUEUE.put_nowait(text)
    except queue.Full:
        pass


def _record_block(stage: str, symbol: str) -> None:
    global _LAST_EVENT, _LAST_SUMMARY_BLOCKS
    with _LOCK:
        _BLOCKS[stage] += 1
        _SYMBOLS[symbol] += 1
        _LAST_EVENT = {"ts": time.time(), "symbol": symbol, "stage": stage, "type": "blocked"}
        blocked = sum(_BLOCKS.values())
        due = blocked - _LAST_SUMMARY_BLOCKS >= _SUMMARY_EVERY
        if due:
            _LAST_SUMMARY_BLOCKS = blocked
            snap = _snapshot_locked()
        else:
            snap = None
    if snap:
        _enqueue_summary(_summary_text(snap))


def observe(message: str) -> None:
    global _CANDIDATES, _AI_REACHED, _AI_APPROVED, _AI_REJECTED, _LAST_EVENT
    if not message:
        return

    for stage, rx in _RULES:
        m = rx.match(message)
        if m:
            _record_block(stage, m.group("symbol"))
            return

    m = _CANDIDATE_RE.match(message)
    if m:
        with _LOCK:
            _CANDIDATES += 1
            _LAST_EVENT = {"ts": time.time(), "symbol": m.group("symbol"), "stage": "CANDIDATE", "type": "pass"}
        return

    m = _AI_RE.match(message)
    if m:
        approved = m.group("approved").lower() == "true"
        with _LOCK:
            _AI_REACHED += 1
            if approved:
                _AI_APPROVED += 1
            else:
                _AI_REJECTED += 1
            _LAST_EVENT = {
                "ts": time.time(), "symbol": m.group("symbol"),
                "stage": "NEXUS_AI", "type": "approved" if approved else "rejected",
            }


class _FunnelHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            observe(record.getMessage())
        except Exception:
            # Telemetry must never affect the trading loop.
            pass


def install(logger: logging.Logger) -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    handler = _FunnelHandler(level=logging.DEBUG)
    handler.set_name("nexus-funnel-metrics")
    logger.addHandler(handler)
    _INSTALLED = True
