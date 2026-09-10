"""Fail-accurate runtime mode/readiness observability.

This module is diagnostic only. It does not grant execution permission,
change PAPER/LIVE selection, disable validation locks, or mutate exchange
state. It centralizes the semantics used by health/status/startup messages so
operator-facing telemetry cannot claim LIVE execution while a safety lock is
blocking mutations.
"""
from __future__ import annotations


def snapshot(*, paper_trade: bool, engine=None, blocked: bool = False,
             mode_reason: str = "") -> dict:
    validation_lock = bool(
        getattr(engine, "_validation_safety_lock_active", False)
        or getattr(type(engine), "_validation_safety_lock_patched", False)
    ) if engine is not None else False

    connected = bool(getattr(engine, "connected", False)) if engine is not None else False
    active = bool(getattr(engine, "active", False)) if engine is not None else False
    shadow_ready = bool(
        getattr(engine, "_shadow_prelive_readonly_ready", False)
    ) if engine is not None else False

    if paper_trade:
        trading_mode = "PAPER"
        orders_sent = False
        trading_ready = connected and active and not blocked
    elif validation_lock:
        trading_mode = "SHADOW_LIVE"
        orders_sent = False
        trading_ready = connected and active and shadow_ready and not blocked
    else:
        trading_mode = "LIVE"
        # This is observability, not authorization: if no validation lock is
        # installed, report exchange-dispatch capability only when the engine
        # is actually connected/active and startup is not blocked.
        orders_sent = connected and active and not blocked
        trading_ready = orders_sent

    return {
        "trading_mode": trading_mode,
        "orders_sent_to_exchange": bool(orders_sent),
        "ready": bool(trading_ready),
        "connected": connected,
        "active": active,
        "validation_lock": validation_lock,
        "shadow_readonly_ready": shadow_ready,
        "mode_reason": mode_reason,
    }


def startup_message(state: dict) -> str:
    mode = state["trading_mode"]
    if mode == "PAPER":
        reason = state.get("mode_reason") or "PAPER_TRADE ativo"
        return (
            "🟡 *BOT ONLINE — MODO SIMULAÇÃO*\n"
            "`━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
            "⚠️ *NENHUMA ordem será enviada à KuCoin*\n\n"
            f"Motivo: _{reason}_"
        )
    if mode == "SHADOW_LIVE":
        return (
            "🟠 *BOT ONLINE — SHADOW LIVE*\n"
            "`━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
            "🔒 *Mutações na KuCoin estão bloqueadas pelo VALIDATION_LOCK*\n"
            "Dados de mercado/conta são reais, mas `execution_effect=NONE`."
        )
    return (
        "🔴 *BOT ONLINE — LIVE*\n"
        "`━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        "Execução na exchange só é considerada disponível enquanto o engine "
        "estiver conectado, ativo e sem bloqueio de startup."
    )
