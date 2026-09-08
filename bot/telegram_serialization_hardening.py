"""Global Telegram delivery serialization for NEXUS-7 observability.

This module is transport-only. It does not alter trading decisions, risk,
sizing, exchange state, order dispatch, or release gates.

The legacy notifier owns global rate-limit state (_last_sent_time/hash) but its
notify coroutine can be entered concurrently by unrelated callers. Under scan
bursts, those callers can race the shared timestamp and trigger Telegram 429
backoff. This wrapper gives every Telegram message one event-loop-local lane.
NEXUS terminal delivery can use the same lane while starting its active-send
timeout only after acquiring it.
"""
from __future__ import annotations

import asyncio


_lock = None
_lock_loop = None
_original_notify = None


def get_lock() -> asyncio.Lock:
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock = asyncio.Lock()
        _lock_loop = loop
    return _lock


def original_notify():
    return _original_notify


async def run_serialized(factory, *, timeout: float | None = None):
    """Run one Telegram send in the global lane.

    Queue wait is intentionally outside ``timeout``. If a timeout is supplied,
    it measures only the active send after the caller acquires the lane.
    """
    async with get_lock():
        coro = factory()
        if timeout is None:
            return await coro
        return await asyncio.wait_for(coro, timeout=timeout)


def install(notifier, log) -> None:
    global _original_notify
    if getattr(notifier, "_bgx_global_telegram_serialized", False):
        return

    _original_notify = notifier.notify

    async def _serialized_notify(text: str):
        return await run_serialized(lambda: _original_notify(text))

    notifier.notify = _serialized_notify
    notifier._bgx_global_telegram_serialized = True
    notifier._bgx_original_notify = _original_notify
    notifier._bgx_run_serialized = run_serialized
    log.info(
        "[TELEGRAM_SERIALIZATION] global delivery lane installed; "
        "queue wait excluded from active send timeout; trading logic unchanged"
    )
