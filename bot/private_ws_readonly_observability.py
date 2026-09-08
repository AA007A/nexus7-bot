"""Read-only private WebSocket readiness observability for SHADOW LIVE.

This module never submits/cancels orders, changes leverage/stops, or grants
execution permission. It performs a one-shot authenticated KuCoin private WS
subscription handshake using the existing prelive read-only probe and stores
only diagnostic state on the client instance.
"""

import asyncio


async def run(client, instruments, log) -> bool:
    from bot.prelive_readonly_probe import _private_ws_probe

    symbol = "ETHUSDT" if "ETHUSDT" in instruments else next(iter(instruments), "")
    if not symbol:
        setattr(client, "_prelive_private_ws_probe_ok", False)
        log.warning("[PRIVATE_WS_READONLY_PROBE] result=FAIL reason=no_instrument execution_effect=NONE")
        return False

    try:
        ok = bool(await asyncio.wait_for(_private_ws_probe(client, symbol), timeout=20.0))
    except Exception as exc:
        setattr(client, "_prelive_private_ws_probe_ok", False)
        log.warning(
            "[PRIVATE_WS_READONLY_PROBE] result=FAIL symbol=%s reason=%s execution_effect=NONE",
            symbol,
            type(exc).__name__,
        )
        return False

    setattr(client, "_prelive_private_ws_probe_ok", ok)
    log.info(
        "[PRIVATE_WS_READONLY_PROBE] result=%s symbol=%s authenticated=true subscription_ack=%s execution_effect=NONE",
        "PASS" if ok else "FAIL",
        symbol,
        str(ok).lower(),
    )
    return ok
