"""Replay-relevant public WebSocket filter for runtime-truth capture.

The live public KuCoin connection carries both replay-critical kline updates and
high-frequency ticker traffic. Runtime truth needs every kline update that can
mutate strategy-visible candle state, but ticker messages don't mutate the
kline cache or Analyzer inputs and were producing an unsustainable research
storage rate during the first production canary.

This module wraps only the telemetry capture function. It never changes the
WebSocket frame delivered to KuCoinClient, never changes cache mutation logic,
and is a no-op unless runtime truth is enabled.
"""
from __future__ import annotations

import json
from typing import Any

_CONTROL_TYPES = {"ack", "error", "welcome"}


def _parse(payload: str | bytes) -> dict[str, Any] | None:
    try:
        if isinstance(payload, bytes):
            text = payload.decode("utf-8", errors="strict")
        else:
            text = str(payload)
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def replay_relevant(payload: str | bytes) -> bool:
    """Return True only for frames needed to prove kline/runtime chronology.

    Captured:
      * every valid Classic Futures kline update consumed by the production
        candle parser;
      * low-volume subscription/session control evidence: ack, error, welcome.

    Intentionally excluded:
      * tickerV2 and other non-kline market feeds;
      * pong traffic;
      * malformed/unknown frames that cannot mutate the current kline cache.

    Excluded frames don't allocate runtime-truth sequence numbers, so deliberate
    filtering cannot create false manifest gaps.
    """
    parsed = _parse(payload)
    if parsed is None:
        return False

    message_type = str(parsed.get("type") or "")
    if message_type in _CONTROL_TYPES:
        return True

    topic = str(parsed.get("topic") or "")
    data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
    candles = data.get("candles")
    return (
        "andle" in topic
        and isinstance(candles, (list, tuple))
        and len(candles) >= 7
    )


def install() -> bool:
    """Install the filter around runtime_truth capture when telemetry is on."""
    from bot import runtime_truth

    if not runtime_truth.enabled():
        return False
    if getattr(runtime_truth, "_ws_replay_filter_installed", False):
        return False

    original = runtime_truth.capture_ws_application_payload

    def filtered(payload: str | bytes, connection_session_id: str):
        if not replay_relevant(payload):
            return None
        return original(payload, connection_session_id)

    runtime_truth.capture_ws_application_payload = filtered
    runtime_truth._ws_replay_filter_installed = True
    return True
