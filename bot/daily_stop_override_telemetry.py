"""Truthful operator-facing daily-stop telemetry for operator overrides.

This module changes notification wording only. It does not clear stop state,
authorize entries, alter PnL, change risk limits, or mutate exchange state.
Actual entry authorization remains owned by ``bot.durable_daily_stop``.

A daily-stop override no longer authorizes entries (override_effect=NONE).
When ``DAILY_STOP_OVERRIDE_UTC_DAY`` names today, the daily-stop notification
keeps its "entries blocked" text and adds an explicit note that the requested
override was ignored, so the operator is never told entries are allowed.
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
_IGNORED_NOTE = (
    "\nOverride do operador solicitado para hoje foi IGNORADO: overrides não "
    "autorizam nova exposição; novas entradas continuam bloqueadas."
)


def persistent_override_requested() -> bool:
    return str(os.environ.get(_PERSISTENT_OVERRIDE_ENV, "") or "").strip().lower() == "true"


def override_mode_now(*, now: datetime | None = None) -> str | None:
    """Return date-scoped mode only when the exact UTC-day override is active."""
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
    """Annotate the LIVE daily-stop notification when an override was requested."""
    original = str(text)
    if not override_active_now(now=now):
        return original, False
    if not original.startswith(_DAILY_STOP_PREFIX):
        return original, False
    if _BLOCKED_TEXT not in original:
        return original, False
    return original + _IGNORED_NOTE, True


def install(core_engine, log) -> None:
    """Wrap the engine's notification callable without changing trading semantics."""
    if getattr(core_engine, "_daily_stop_override_telemetry_installed", False):
        return

    previous_notify = core_engine.notify

    async def _truthful_notify(text: str):
        rewritten, changed = truthful_message(text)
        if changed:
            log.warning(
                "[DAILY_STOP_OVERRIDE_ALERT] mode=%s override_requested=true override_effect=NONE "
                "entries_blocked=true pnl_preserved=true evidence_preserved=true",
                override_mode_now(),
            )
        return await previous_notify(rewritten)

    core_engine.notify = _truthful_notify
    core_engine._daily_stop_override_telemetry_installed = True
    log.info(
        "[DAILY_STOP_OVERRIDE_TELEMETRY] installed=true notification_only=true "
        "persistent_mode_retired=true date_scoped_mode_supported=true pnl_unchanged=true "
        "risk_policy_unchanged=true entry_authorization_unchanged=true execution_effect=NONE"
    )
