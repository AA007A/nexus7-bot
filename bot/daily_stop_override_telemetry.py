"""Truthful operator-facing daily-stop telemetry for operator overrides.

This module changes notification wording only. It does not clear stop state,
authorize entries, alter PnL, change risk limits, or mutate exchange state.
Actual entry authorization remains owned by ``bot.durable_daily_stop``.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

_OVERRIDE_ENV = "DAILY_STOP_OVERRIDE_UTC_DAY"
_PERSISTENT_OVERRIDE_ENV = "DAILY_STOP_OPERATOR_OVERRIDE"
_DAILY_STOP_PREFIX = "🛑 *Stop-Loss DIÁRIO*"
_BLOCKED_TEXT = (
    "Novas entradas bloqueadas; posições abertas continuam sendo gerenciadas e protegidas."
)
_ALLOWED_TEXT = (
    "Novas entradas permanecem liberadas pelo override do operador; "
    "posições abertas continuam sendo gerenciadas e protegidas."
)


def override_mode_now(*, now: datetime | None = None) -> str | None:
    """Return persistent/date-scoped mode when the operator override is active."""
    persistent = str(os.environ.get(_PERSISTENT_OVERRIDE_ENV, "") or "").strip().lower()
    if persistent == "true":
        return "persistent"

    raw = str(os.environ.get(_OVERRIDE_ENV, "") or "").strip()
    if not raw:
        return None
    try:
        configured_day = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return "date_scoped" if configured_day == current.astimezone(timezone.utc).date() else None


def override_active_now(*, now: datetime | None = None) -> bool:
    return override_mode_now(now=now) is not None


def truthful_message(text: str, *, now: datetime | None = None) -> tuple[str, bool]:
    """Rewrite only the misleading LIVE daily-stop notification when override is active."""
    original = str(text)
    if not override_active_now(now=now):
        return original, False
    if not original.startswith(_DAILY_STOP_PREFIX):
        return original, False
    if _BLOCKED_TEXT not in original:
        return original, False

    rewritten = original.replace(
        _DAILY_STOP_PREFIX,
        "⚠️ *Limite diário excedido — override do operador ativo*",
        1,
    ).replace(_BLOCKED_TEXT, _ALLOWED_TEXT, 1)
    return rewritten, True


def install(core_engine, log) -> None:
    """Wrap the engine's notification callable without changing trading semantics."""
    if getattr(core_engine, "_daily_stop_override_telemetry_installed", False):
        return

    previous_notify = core_engine.notify

    async def _truthful_notify(text: str):
        rewritten, changed = truthful_message(text)
        if changed:
            log.warning(
                "[DAILY_STOP_OVERRIDE_ALERT] mode=%s override_active=true entries_blocked=false "
                "pnl_preserved=true evidence_preserved=true execution_effect=NONE",
                override_mode_now(),
            )
        return await previous_notify(rewritten)

    core_engine.notify = _truthful_notify
    core_engine._daily_stop_override_telemetry_installed = True
    log.info(
        "[DAILY_STOP_OVERRIDE_TELEMETRY] installed=true notification_only=true "
        "persistent_mode_supported=true pnl_unchanged=true risk_policy_unchanged=true "
        "entry_authorization_unchanged=true execution_effect=NONE"
    )
