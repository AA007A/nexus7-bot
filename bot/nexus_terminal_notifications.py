"""Terminal Telegram and log observability for NEXUS AI decisions.

This module is notification/telemetry-only. It does not change scores,
thresholds, execution authorization, order sizing, risk, or exchange behavior.

The core engine already sends approval notifications after the validated NEXUS
approval gate. This hardening closes the observability gap for outcomes that
previously returned silently from ``_open``: REJECT, TIMEOUT and ERROR, and it
prevents early fail-closed vetoes from being logged as measured zero scores.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time


_terminal_cache: dict[tuple[str, str, str], float] = {}
_TERMINAL_COOLDOWN = int(os.environ.get("NEXUS_TERMINAL_COOLDOWN", "120"))

# The canonical engine emits [AI_DECISION] immediately after _nexus_validate
# returns. Cache the just-computed observational snapshot so a logging filter can
# enrich that line without touching the decision object or execution flow.
_ai_telemetry_cache: dict[str, dict] = {}
_ai_telemetry_lock = threading.Lock()
_ai_log_filter_installed = False


def _decision_value(decision, name: str, default=None):
    try:
        value = getattr(decision, name, default)
        return default if value is None else value
    except Exception:
        return default


def _reason_from_decision(decision, validation_reason: str | None) -> str:
    if validation_reason:
        return str(validation_reason)
    try:
        reasoning = list(getattr(decision, "reasoning", None) or [])
        if reasoning:
            return str(reasoning[-1])
    except Exception:
        # Telemetry-only fallback: an unreadable reasoning object is equivalent
        # to no reasoning being available and must not affect execution.
        reasoning = []
    return "NEXUS não autorizou a execução"


def _reasoning_text(decision) -> str:
    try:
        return " | ".join(str(x) for x in (getattr(decision, "reasoning", None) or []))
    except Exception:
        return ""


def _extract_float(text: str, pattern: str):
    match = re.search(pattern, text or "", flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return None


def _meaningful_numeric(decision, name: str):
    """Return a measured metric or None when 0.0 is only a dataclass default.

    Several fail-closed exits happen before final NEXUS scoring. NexusDecision
    deliberately defaults those not-yet-computed metrics to 0.0. Reporting the
    defaults as measured values is misleading, so zero is treated as unavailable
    for reject observability and already-computed values are recovered from the
    reasoning text when possible.
    """
    value = _decision_value(decision, name, None)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value != 0.0 else None


def _reject_metrics(sig, decision, reason: str) -> dict:
    reasoning = _reasoning_text(decision)
    combined = f"{reason} | {reasoning}"

    candidate_score = None
    try:
        candidate_score = float(getattr(sig, "score", None))
    except (TypeError, ValueError):
        # Optional display metric only. Invalid or absent candidate score is
        # rendered as unavailable; it cannot change the NEXUS decision.
        candidate_score = None

    nexus_score = _meaningful_numeric(decision, "setup_quality")
    confidence = _meaningful_numeric(decision, "confidence")
    if confidence is None:
        confidence = _extract_float(combined, r"\bconf(?:idence)?\s*[=:]\s*([+-]?\d+(?:\.\d+)?)")

    rr = _meaningful_numeric(decision, "risk_reward")
    if rr is None:
        rr = _extract_float(combined, r"R:R\s*l[ií]quido\s*([+-]?\d+(?:\.\d+)?)")

    ev = _meaningful_numeric(decision, "expected_value")
    if ev is None:
        ev = _extract_float(combined, r"\bEV(?:\s+negativo\s+ap[oó]s\s+custos)?[:\s]+([+-]?\d+(?:\.\d+)?)%")

    return {
        "candidate_score": candidate_score,
        "nexus_score": nexus_score,
        "confidence": confidence,
        "rr": rr,
        "ev": ev,
    }


def _classify_stage(reason: str) -> str:
    text = (reason or "").lower()
    if any(k in text for k in ("qualidade de dados", "candles", "dados insuficientes", "dados antigos", "data_unavailable")):
        return "DATA_GATE"
    if any(k in text for k in ("conflito entre timeframes", "diverge do mtf", "mtf")):
        return "MTF_GATE"
    if any(k in text for k in ("extreme_event", "incompatível com regime", "regime")):
        return "REGIME_GATE"
    if any(k in text for k in ("ensemble sem direção", "ensemble (", "modelos")):
        return "ENSEMBLE_GATE"
    if any(k in text for k in ("entrada/sl/tp", "stop inválido", "níveis de entrada")):
        return "LEVELS_GATE"
    if any(k in text for k in ("ev negativo", "r:r líquido")):
        return "EV_RR_GATE"
    if "score" in text:
        return "FINAL_SCORE_GATE"
    return "EARLY_VETO_OTHER"


def _fmt_log_metric(value, fmt: str = ".3f") -> str:
    if value is None:
        return "N/A"
    return format(float(value), fmt)


def _cache_ai_telemetry(sig, decision, validation_reason: str | None) -> None:
    reason = _reason_from_decision(decision, validation_reason)
    metrics = _reject_metrics(sig, decision, reason)
    approved = validation_reason is None and getattr(decision, "execution_allowed", None) is True
    if approved:
        stage = "APPROVED"
        # For an approved decision all final fields are meaningful, including a
        # theoretical zero EV if the model ever allows it.
        metrics = {
            "candidate_score": float(getattr(sig, "score", 0.0) or 0.0),
            "nexus_score": float(getattr(decision, "setup_quality", 0.0) or 0.0),
            "confidence": float(getattr(decision, "confidence", 0.0) or 0.0),
            "rr": float(getattr(decision, "risk_reward", 0.0) or 0.0),
            "ev": float(getattr(decision, "expected_value", 0.0) or 0.0),
        }
    else:
        stage = _classify_stage(reason)

    snap = {
        **metrics,
        "stage": stage,
        "reason": " ".join(str(reason).split())[:220],
        "ts": time.time(),
    }
    with _ai_telemetry_lock:
        _ai_telemetry_cache[str(getattr(sig, "symbol", "?"))] = snap
        # Keep the cache bounded and short-lived; it exists only to bridge the
        # return from _nexus_validate to the immediately following log line.
        cutoff = snap["ts"] - 30.0
        for symbol, cached in list(_ai_telemetry_cache.items()):
            if cached.get("ts", 0.0) < cutoff:
                _ai_telemetry_cache.pop(symbol, None)


def _take_ai_telemetry(symbol: str):
    with _ai_telemetry_lock:
        snap = _ai_telemetry_cache.pop(symbol, None)
    if not snap or time.time() - snap.get("ts", 0.0) > 30.0:
        return None
    return snap


_AI_LINE_RE = re.compile(
    r"^\[AI_DECISION\]\s+symbol=(?P<symbol>\S+)\s+side=(?P<side>\S+)\s+"
    r"decision=(?P<decision>\S+)\s+approved=(?P<approved>\S+)\s+"
    r"decision_source=(?P<source>\S+).*?\s+ts=(?P<ts>\d+)\s+reason=.*$"
)


class _TruthfulAIDecisionFilter(logging.Filter):
    """Rewrite only the observational AI_DECISION line with truthful metrics."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            match = _AI_LINE_RE.match(message)
            if not match or match.group("source") != "nexus_ai":
                return True
            snap = _take_ai_telemetry(match.group("symbol"))
            if not snap:
                return True

            record.msg = (
                f"[AI_DECISION] symbol={match.group('symbol')} side={match.group('side')} "
                f"decision={match.group('decision')} approved={match.group('approved')} "
                f"decision_source=nexus_ai "
                f"candidate_score={_fmt_log_metric(snap['candidate_score'], '.1f')} "
                f"nexus_score={_fmt_log_metric(snap['nexus_score'], '.1f')} "
                f"confidence={_fmt_log_metric(snap['confidence'], '.1f')} "
                f"rr_net={_fmt_log_metric(snap['rr'], '.2f')} "
                f"ev={_fmt_log_metric(snap['ev'], '+.3f')} "
                f"stage={snap['stage']} ts={match.group('ts')} "
                f"reason={snap['reason']}"
            )
            record.args = ()
        except Exception:
            # Observability can never break the logger or trading loop.
            return True
        return True


def _install_ai_log_filter(log) -> None:
    global _ai_log_filter_installed
    if _ai_log_filter_installed:
        return
    try:
        handlers = list(getattr(log, "handlers", []) or [])
        if not handlers:
            return
        filt = _TruthfulAIDecisionFilter()
        for handler in handlers:
            handler.addFilter(filt)
        _ai_log_filter_installed = True
    except Exception:
        return


def _fmt_metric(value, fmt: str, unavailable: str = "—") -> str:
    if value is None:
        return unavailable
    return format(value, fmt)


def _dedupe_ok(symbol: str, status: str, reason: str) -> bool:
    now = time.time()
    key = (symbol, status, reason[:80])
    last = _terminal_cache.get(key, 0.0)
    if now - last < _TERMINAL_COOLDOWN:
        return False
    _terminal_cache[key] = now
    if len(_terminal_cache) > 500:
        for old_key, ts in list(_terminal_cache.items()):
            if now - ts > _TERMINAL_COOLDOWN * 3:
                _terminal_cache.pop(old_key, None)
    return True


async def _notify_reject(notifier, sig, decision, validation_reason: str | None) -> None:
    reason = _reason_from_decision(decision, validation_reason)
    if not _dedupe_ok(sig.symbol, "REJECT", reason):
        return

    metrics = _reject_metrics(sig, decision, reason)
    candidate_score = _fmt_metric(metrics["candidate_score"], ".1f")
    nexus_score = _fmt_metric(
        metrics["nexus_score"], ".1f", "não calculado (veto anterior ao score final)"
    )
    confidence = _fmt_metric(metrics["confidence"], ".1f")
    rr = _fmt_metric(metrics["rr"], ".2f")
    ev = _fmt_metric(metrics["ev"], "+.3f")
    ev_suffix = "%" if metrics["ev"] is not None else ""

    await notifier.notify(
        f"🚫 *NEXUS AI — REJEITADO*\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        f"📍 Par: `{sig.symbol}`\n"
        f"🧭 Direção: `{sig.direction}`\n"
        f"🧠 Score candidato: `{candidate_score}/100`\n"
        f"🧠 Score NEXUS: `{nexus_score}`\n"
        f"🎯 Confiança: `{confidence}{'%' if metrics['confidence'] is not None else ''}`\n"
        f"⚖️ R:R líquido: `{rr}`\n"
        f"📈 EV: `{ev}{ev_suffix}`\n"
        f"❌ Motivo: _{reason[:180]}_\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        f"_Nenhuma ordem foi enviada._"
    )


async def _notify_failure(notifier, sig, status: str, reason: str) -> None:
    if not _dedupe_ok(sig.symbol, status, reason):
        return
    icon = "⏱️" if status == "TIMEOUT" else "⚠️"
    title = "TIMEOUT" if status == "TIMEOUT" else "ERRO"
    await notifier.notify(
        f"{icon} *NEXUS AI — {title}*\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        f"📍 Par: `{sig.symbol}`\n"
        f"🧭 Direção: `{sig.direction}`\n"
        f"❌ Motivo: `{reason[:120]}`\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        f"_Falha fechada: nenhuma ordem foi enviada._"
    )


def install(TradingEngine, notifier, nexus_types, log) -> None:
    if getattr(TradingEngine, "_nexus_terminal_notifications_installed", False):
        return

    _install_ai_log_filter(log)
    original_validate = TradingEngine._nexus_validate

    async def _nexus_validate_with_terminal_notification(self, sig):
        try:
            decision = await original_validate(self, sig)
            validation_reason = nexus_types.decision_validation_error(
                decision, sig.symbol, sig.direction, sig.entry, sig.sl, sig.tp
            )
            approved = (
                validation_reason is None
                and getattr(decision, "execution_allowed", None) is True
            )
            _cache_ai_telemetry(sig, decision, validation_reason)
            if not approved:
                asyncio.create_task(
                    _notify_reject(notifier, sig, decision, validation_reason)
                )
            return decision
        except asyncio.CancelledError:
            asyncio.create_task(
                _notify_failure(notifier, sig, "TIMEOUT", "ai_timeout")
            )
            raise
        except Exception as exc:
            asyncio.create_task(
                _notify_failure(notifier, sig, "ERROR", type(exc).__name__)
            )
            raise

    TradingEngine._nexus_validate = _nexus_validate_with_terminal_notification
    TradingEngine._nexus_terminal_notifications_installed = True
    log.info(
        "[NEXUS_TERMINAL_TELEGRAM] installed: truthful REJECT/TIMEOUT/ERROR "
        "Telegram + AI_DECISION partial-metric telemetry; "
        "decision_effect=NONE execution_effect=NONE"
    )
