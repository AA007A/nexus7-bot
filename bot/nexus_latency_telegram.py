"""SHADOW-safe NEXUS AI latency telemetry and terminal Telegram reporting.

This module adds observability only. It does not alter AI approval criteria,
execution gates, risk limits, sizing, exchange state, or order dispatch.
"""
import asyncio
import time


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


def install(TradingEngine, notifier, log):
    """Instrument ``_nexus_validate`` and guarantee a terminal SHADOW message."""
    if getattr(TradingEngine, "_nexus_latency_telegram_patched", False):
        return

    original_validate = TradingEngine._nexus_validate

    async def _validate_with_latency(self, sig, *args, **kwargs):
        started = time.monotonic()
        symbol = getattr(sig, "symbol", "?")
        log.info("[NEXUS_LATENCY] symbol=%s stage=started", symbol)
        try:
            decision = await original_validate(self, sig, *args, **kwargs)
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
            try:
                await notifier.notify(_terminal_message(data, approved, elapsed))
                log.info(
                    "[NEXUS_TELEGRAM_TERMINAL] symbol=%s approved=%s elapsed_ms=%d sent=true",
                    symbol, approved, int(elapsed * 1000),
                )
            except Exception as exc:
                log.warning(
                    "[NEXUS_TELEGRAM_TERMINAL] symbol=%s sent=false error=%s",
                    symbol, type(exc).__name__,
                )
            return decision
        except asyncio.CancelledError:
            elapsed = time.monotonic() - started
            log.warning(
                "[NEXUS_LATENCY] symbol=%s stage=cancelled elapsed_ms=%d",
                symbol, int(elapsed * 1000),
            )
            try:
                await notifier.notify(_failure_message(symbol, "timeout/cancelled", elapsed))
            except Exception as notify_exc:
                log.warning(
                    "[NEXUS_TELEGRAM_TERMINAL] symbol=%s failure_notice=false stage=cancelled error=%s",
                    symbol, type(notify_exc).__name__,
                )
            raise
        except Exception as exc:
            elapsed = time.monotonic() - started
            log.warning(
                "[NEXUS_LATENCY] symbol=%s stage=failed error=%s elapsed_ms=%d",
                symbol, type(exc).__name__, int(elapsed * 1000),
            )
            try:
                await notifier.notify(
                    _failure_message(symbol, type(exc).__name__, elapsed)
                )
            except Exception as notify_exc:
                log.warning(
                    "[NEXUS_TELEGRAM_TERMINAL] symbol=%s failure_notice=false stage=failed error=%s",
                    symbol, type(notify_exc).__name__,
                )
            raise

    TradingEngine._nexus_validate = _validate_with_latency
    TradingEngine._nexus_latency_telegram_patched = True
    log.info(
        "[NEXUS_LATENCY] terminal Telegram observability installed; trading logic unchanged"
    )
