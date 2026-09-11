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
import time


_terminal_cache: dict[tuple[str, str, str], float] = {}
_TERMINAL_COOLDOWN = int(os.environ.get("NEXUS_TERMINAL_COOLDOWN", "120"))


def _decision_value(decision, name: str, default=0):
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

    score = float(_decision_value(decision, "setup_quality", 0) or 0)
    confidence = float(_decision_value(decision, "confidence", 0) or 0)
    rr = float(_decision_value(decision, "risk_reward", 0) or 0)
    ev = float(_decision_value(decision, "expected_value", 0) or 0)

    await notifier.notify(
        f"🚫 *NEXUS AI — REJEITADO*\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        f"📍 Par: `{sig.symbol}`\n"
        f"🧭 Direção: `{sig.direction}`\n"
        f"🧠 Score final: `{score:.1f}/100`\n"
        f"🎯 Confiança: `{confidence:.1f}%`\n"
        f"⚖️ R:R líquido: `{rr:.2f}`\n"
        f"📈 EV: `{ev:+.3f}%`\n"
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
            # asyncio.wait_for cancels _nexus_validate when the engine timeout
            # expires. Emit the terminal status before propagating cancellation.
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
        "notifications; decision_effect=NONE execution_effect=NONE"
    )
