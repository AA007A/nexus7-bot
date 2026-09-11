"""Terminal Telegram observability for NEXUS AI decisions.

This module is notification-only. It does not change scores, thresholds,
execution authorization, order sizing, risk, or exchange behavior.

The core engine already sends approval notifications after the validated NEXUS
approval gate. This hardening closes the observability gap for outcomes that
previously returned silently from ``_open``: REJECT, TIMEOUT and ERROR.
"""
from __future__ import annotations

import asyncio
import os
import re
import time


_terminal_cache: dict[tuple[str, str, str], float] = {}
_TERMINAL_COOLDOWN = int(os.environ.get("NEXUS_TERMINAL_COOLDOWN", "120"))


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
        pass
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
    """Return a real metric or None when the field is only a dataclass default.

    Several fail-closed exits happen before final NEXUS scoring. NexusDecision
    deliberately defaults those not-yet-computed metrics to 0.0. Reporting the
    defaults as measured values is misleading, so zero is treated as unavailable
    for reject observability and we recover already-computed values from the
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
        pass

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
        "[NEXUS_TERMINAL_TELEGRAM] installed: REJECT/TIMEOUT/ERROR final "
        "notifications with truthful partial-metric display; "
        "decision_effect=NONE execution_effect=NONE"
    )
