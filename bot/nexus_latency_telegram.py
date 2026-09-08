"""SHADOW-safe NEXUS AI latency telemetry and terminal Telegram reporting.

This module adds observability only. It does not alter AI approval criteria,
execution gates, risk limits, sizing, exchange state, or order dispatch.

Terminal Telegram delivery is intentionally detached from the NEXUS validation
coroutine. This guarantees that notifier latency cannot consume the caller's
AI timeout budget and cannot turn an already-finished PASS/VETO into a later
``timeout/cancelled`` outcome for the same analysis.
"""
import asyncio
import time


# notifier.notify may legitimately spend up to ~3s in global rate limiting and
# up to 10s in one HTTP request. Keep this wrapper budget above that combined
# path so it reports genuine delivery stalls instead of expected notifier wait.
_NOTIFY_TIMEOUT_S = 15.0


def _reason_from_decision(data: dict) -> str:
    reasoning = data.get("reasoning") or []
    warnings = data.get("warnings") or []
    if reasoning:
        return str(reasoning[-1])[:180]
    if warnings:
        return str(warnings[0])[:180]
    return "sem motivo detalhado"


def _terminal_message(data: dict, approved: bool, elapsed_s: float) -> str:
    symbol = data.get("symbol", "?")
    status = "APROVADO" if approved else "VETO"
    icon = "✅" if approved else "🚫"
    regime = data.get("market_regime") or "?"
    rr = data.get("risk_reward")
    ev = data.get("expected_value")
    reason = _reason_from_decision(data)

    rr_line = f"⚖️ R:R líquido: `{float(rr):.2f}`\n" if rr is not None else ""
    ev_line = f"📈 EV: `{float(ev):+.3f}%`\n" if ev is not None else ""
    return (
        f"{icon} *NEXUS AI — RESULTADO {status}*\n"
        f"📍 Par: `{symbol}`\n"
        f"🌐 Regime: `{regime}`\n"
        f"{rr_line}{ev_line}"
        f"⏱️ Análise: `{elapsed_s:.2f}s`\n"
        f"🧠 Motivo: _{reason}_\n"
        f"🔒 SHADOW: `execution_effect=NONE`"
    )


def _failure_message(symbol: str, kind: str, elapsed_s: float) -> str:
    return (
        f"⚠️ *NEXUS AI — ANÁLISE NÃO CONCLUÍDA*\n"
        f"📍 Par: `{symbol}`\n"
        f"🧩 Motivo: `{kind}`\n"
        f"⏱️ Tempo: `{elapsed_s:.2f}s`\n"
        f"🔒 Fail-closed: `nenhuma ordem enviada`"
    )


def _spawn_notice(coro, *, symbol: str, stage: str, log):
    """Run notifier delivery outside validation and coalesce overlaps.

    A fast scan loop can finish a second NEXUS evaluation for the same symbol
    while the previous Telegram request is still rate-limited or in flight.
    Starting another delivery in that situation only creates notifier backlog
    and increases the chance of HTTP 429 / wrapper timeouts. Observability is
    best-effort, so keep at most one terminal delivery per (symbol, stage) in
    flight. This does not alter, delay, or reclassify the NEXUS decision itself.
    """
    key = (str(symbol), str(stage))
    inflight = getattr(_spawn_notice, "_inflight", None)
    if inflight is None:
        inflight = {}
        _spawn_notice._inflight = inflight

    existing = inflight.get(key)
    if existing is not None and not existing.done():
        # The coroutine object was created by the caller; close it explicitly
        # because this best-effort duplicate will not be awaited or scheduled.
        close = getattr(coro, "close", None)
        if close is not None:
            close()
        log.debug(
            "[NEXUS_TELEGRAM_TERMINAL] symbol=%s stage=%s delivery_coalesced=true",
            symbol, stage,
        )
        return existing

    async def _runner():
        try:
            await asyncio.wait_for(coro, timeout=_NOTIFY_TIMEOUT_S)
            log.info(
                "[NEXUS_TELEGRAM_TERMINAL] symbol=%s stage=%s sent=true",
                symbol, stage,
            )
        except asyncio.TimeoutError:
            log.warning(
                "[NEXUS_TELEGRAM_TERMINAL] symbol=%s stage=%s sent=false error=notify_timeout",
                symbol, stage,
            )
        except asyncio.CancelledError:
            # Process shutdown is not a second AI terminal outcome.
            log.debug(
                "[NEXUS_TELEGRAM_TERMINAL] symbol=%s stage=%s delivery_cancelled",
                symbol, stage,
            )
            raise
        except Exception as exc:
            log.warning(
                "[NEXUS_TELEGRAM_TERMINAL] symbol=%s stage=%s sent=false error=%s",
                symbol, stage, type(exc).__name__,
            )

    task = asyncio.create_task(_runner())
    # Keep a strong reference until completion; otherwise fire-and-forget tasks
    # can be collected before they run on some event-loop lifecycles.
    pending = getattr(_spawn_notice, "_pending", None)
    if pending is None:
        pending = set()
        _spawn_notice._pending = pending
    pending.add(task)
    inflight[key] = task

    def _done(done_task):
        pending.discard(done_task)
        if inflight.get(key) is done_task:
            inflight.pop(key, None)

    task.add_done_callback(_done)
    return task


def install(TradingEngine, notifier, log):
    """Instrument ``_nexus_validate`` and guarantee one AI terminal outcome."""
    if getattr(TradingEngine, "_nexus_latency_telegram_patched", False):
        return

    original_validate = TradingEngine._nexus_validate

    async def _validate_with_latency(self, sig, *args, **kwargs):
        started = time.monotonic()
        symbol = getattr(sig, "symbol", "?")
        log.info("[NEXUS_LATENCY] symbol=%s stage=started", symbol)
        try:
            decision = await original_validate(self, sig, *args, **kwargs)
        except asyncio.CancelledError:
            elapsed = time.monotonic() - started
            log.warning(
                "[NEXUS_LATENCY] symbol=%s stage=cancelled elapsed_ms=%d",
                symbol, int(elapsed * 1000),
            )
            _spawn_notice(
                notifier.notify(_failure_message(symbol, "timeout/cancelled", elapsed)),
                symbol=symbol,
                stage="cancelled",
                log=log,
            )
            raise
        except Exception as exc:
            elapsed = time.monotonic() - started
            log.warning(
                "[NEXUS_LATENCY] symbol=%s stage=failed error=%s elapsed_ms=%d",
                symbol, type(exc).__name__, int(elapsed * 1000),
            )
            _spawn_notice(
                notifier.notify(_failure_message(symbol, type(exc).__name__, elapsed)),
                symbol=symbol,
                stage="failed",
                log=log,
            )
            raise

        # From this point forward the NEXUS decision is terminal and must be
        # returned immediately. Telegram serialization/delivery is observability
        # only and therefore cannot be allowed to reclassify the AI outcome.
        elapsed = time.monotonic() - started
        try:
            data = decision.to_dict() if hasattr(decision, "to_dict") else dict(decision)
        except Exception as serialization_exc:
            log.warning(
                "[NEXUS_TELEGRAM_TERMINAL] symbol=%s decision_serialization_failed=%s",
                symbol, type(serialization_exc).__name__,
            )
            data = {"symbol": symbol}
        data.setdefault("symbol", symbol)
        approved = getattr(decision, "execution_allowed", False) is True
        log.info(
            "[NEXUS_LATENCY] symbol=%s stage=finished approved=%s elapsed_ms=%d",
            symbol, approved, int(elapsed * 1000),
        )
        _spawn_notice(
            notifier.notify(_terminal_message(data, approved, elapsed)),
            symbol=symbol,
            stage="finished",
            log=log,
        )
        return decision

    TradingEngine._nexus_validate = _validate_with_latency
    TradingEngine._nexus_latency_telegram_patched = True
    log.info(
        "[NEXUS_LATENCY] terminal Telegram observability installed; "
        "delivery detached/coalesced outside AI timeout budget; trading logic unchanged"
    )
