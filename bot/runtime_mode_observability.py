"""Fail-accurate runtime mode/readiness observability.

This module is diagnostic only. It does not grant execution permission,
change PAPER/LIVE selection, disable validation locks, or mutate exchange
state. It centralizes the semantics used by health/status/startup messages so
operator-facing telemetry cannot claim LIVE execution while a safety lock is
blocking mutations.
"""
from __future__ import annotations

import asyncio
import weakref


_ENGINE_REFS: dict[int, weakref.ReferenceType] = {}
_DEFERRED_TASKS: set[int] = set()
_STARTUP_READY_TIMEOUT_S = 90.0
_STARTUP_READY_POLL_S = 0.5


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

    engine_id = None
    if engine is not None:
        engine_id = id(engine)
        try:
            _ENGINE_REFS[engine_id] = weakref.ref(engine)
        except TypeError:
            engine_id = None

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
        "blocked": bool(blocked),
        "validation_lock": validation_lock,
        "shadow_readonly_ready": shadow_ready,
        "mode_reason": mode_reason,
        "_engine_id": engine_id,
    }


def _engine_runtime_ready(engine) -> bool:
    """Observational startup readiness; never mutates execution state."""
    if engine is None:
        return False
    if not bool(getattr(engine, "connected", False)):
        return False
    if not bool(getattr(engine, "active", False)):
        return False
    viable = getattr(engine, "viable_symbols", None)
    if viable is not None and len(viable) == 0:
        return False
    risk = getattr(engine, "risk", None)
    if risk is not None:
        try:
            if float(getattr(risk, "balance", 0.0) or 0.0) <= 0.0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def final_runtime_message(engine) -> str:
    """Render the settled LIVE state using read-only execution observability."""
    try:
        from bot.status_observability import execution_observability

        obs = execution_observability(engine)
    except Exception:
        obs = {
            "effective_execution_mode": "UNKNOWN_LOCKED",
            "new_entries_allowed": False,
            "execution_blockers": ("STATUS_UNAVAILABLE",),
        }

    mode = str(obs.get("effective_execution_mode", "UNKNOWN_LOCKED"))
    if mode == "LIVE" and bool(obs.get("new_entries_allowed", False)):
        return (
            "🔴 *BOT ONLINE — LIVE OPERACIONAL*\n"
            "`━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
            "✅ Engine conectado e ativo; runtime LIVE estabilizado.\n"
            "✅ Novas entradas não estão bloqueadas pelos gates observados neste instante."
        )

    blockers = tuple(obs.get("execution_blockers", ()) or ())
    blocker_text = ", ".join(blockers) or "RUNTIME_GATE"
    extra = ""
    if "DRAWDOWN_HARD_GATE" in blockers:
        dd = obs.get("drawdown_pct")
        limit = obs.get("drawdown_limit_pct")
        if dd is not None and limit is not None:
            extra = f"\nDrawdown observado: *{dd:.2f}%* | limite: *{limit:.2f}%*."
    return (
        "🟠 *BOT ONLINE — LIVE BLOQUEADO*\n"
        "`━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        "✅ Engine conectado e ativo.\n"
        "🔒 Novas entradas estão bloqueadas por um gate operacional/risco.\n"
        f"Bloqueador: `{blocker_text}`.{extra}"
    )


async def _deferred_final_notification(engine_id: int) -> None:
    """Wait for settled runtime readiness and then emit one definitive banner."""
    try:
        deadline = asyncio.get_running_loop().time() + _STARTUP_READY_TIMEOUT_S
        engine = None
        while asyncio.get_running_loop().time() < deadline:
            ref = _ENGINE_REFS.get(engine_id)
            engine = ref() if ref is not None else None
            if engine is None:
                return
            if _engine_runtime_ready(engine):
                # One extra observation interval prevents a transient flag flip
                # from being announced as settled operational readiness.
                await asyncio.sleep(_STARTUP_READY_POLL_S)
                if _engine_runtime_ready(engine):
                    from bot.notifier import notify

                    await notify(final_runtime_message(engine))
                    return
            await asyncio.sleep(_STARTUP_READY_POLL_S)

        if engine is not None:
            from bot.notifier import notify

            await notify(
                "🟠 *NEXUS-7 — INICIALIZAÇÃO NÃO CONCLUÍDA*\n"
                "`━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
                "O runtime não confirmou engine conectado/ativo dentro da janela de startup.\n"
                "Nenhuma disponibilidade de novas entradas é afirmada."
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        # Telegram/observability must never change trading state.
        return
    finally:
        _DEFERRED_TASKS.discard(engine_id)


def _schedule_deferred_final(state: dict) -> None:
    engine_id = state.get("_engine_id")
    if not isinstance(engine_id, int) or engine_id in _DEFERRED_TASKS:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _DEFERRED_TASKS.add(engine_id)
    loop.create_task(_deferred_final_notification(engine_id))


def startup_message(state: dict) -> str:
    """Render startup telemetry without overstating execution readiness.

    During LIVE bootstrap the first message is explicitly transitional. A
    second, definitive notification is scheduled only after the engine has
    become connected, active and observably initialized.
    """
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

    if not bool(state.get("ready", False)) or not bool(
        state.get("orders_sent_to_exchange", False)
    ):
        _schedule_deferred_final(state)
        return (
            "⏳ *NEXUS-7 INICIALIZANDO*\n"
            "`━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
            "Conectando engine, carregando instrumentos, WebSockets e preflight LIVE.\n"
            "Nenhuma falha de conexão é afirmada durante esta fase transitória.\n"
            "Um segundo aviso será enviado quando o estado operacional estiver estabilizado."
        )

    engine_id = state.get("_engine_id")
    ref = _ENGINE_REFS.get(engine_id) if isinstance(engine_id, int) else None
    engine = ref() if ref is not None else None
    if engine is not None:
        return final_runtime_message(engine)

    return (
        "🔴 *BOT ONLINE — LIVE OPERACIONAL*\n"
        "`━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        "Engine conectado e ativo; o modo LIVE está operacional neste instante.\n"
        "⚠️ Gates dinâmicos de risco e pré-trade continuam podendo bloquear novas entradas."
    )
