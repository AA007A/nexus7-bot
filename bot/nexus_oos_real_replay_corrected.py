"""Canonical entrypoint for historical NEXUS replay with full clock/data parity.

This adapter is research-only. It fixes two historical-replay hazards without
changing production trading behavior:
1) all production freshness checks observe one atomically frozen historical clock;
2) KuCoin history is requested in conservative 200-candle time windows so a
   server-side page cap cannot silently skip the middle of a wider request.
"""
from __future__ import annotations

import asyncio
import time as _stdlib_time

from bot import nexus_oos_real_replay as _core
from bot.backtest import _historical_integrity, _interval_minutes, _kucoin_page


_ORIGINAL_FETCH_HISTORY = _core.fetch_history


def _freeze_full_clock(nexus_ai, decision_ts_ms: int):
    from bot import market_data_integrity

    class _Clock:
        def __enter__(self):
            self._old = _stdlib_time.time
            self._frozen = lambda: decision_ts_ms / 1000.0 + 1.0
            _stdlib_time.time = self._frozen
            assert nexus_ai.time.time is self._frozen
            assert market_data_integrity.time.time is self._frozen
            return self

        def __exit__(self, exc_type, exc, tb):
            _stdlib_time.time = self._old
            assert nexus_ai.time.time is self._old
            assert market_data_integrity.time.time is self._old

    return _Clock()


async def _fetch_history_contiguous(client, symbol: str, interval: str, limit: int = 1000) -> list:
    """Read historical futures bars without allowing server page caps to skip time."""
    if limit <= 0:
        return []
    if not hasattr(client, "_get"):
        return await _ORIGINAL_FETCH_HISTORY(client, symbol, interval, limit)

    interval_ms = _interval_minutes(interval) * 60 * 1000
    page_size = 200
    cursor_end = int(_stdlib_time.time() * 1000)
    by_ts: dict[int, dict] = {}
    previous_oldest: int | None = None
    max_pages = max(2, (limit + page_size - 1) // page_size + 5)

    for _ in range(max_pages):
        if len(by_ts) >= limit:
            break
        requested = min(page_size, limit - len(by_ts))
        cursor_start = max(0, cursor_end - interval_ms * requested)
        page = await _kucoin_page(client, symbol, interval, cursor_start, cursor_end)
        if not page:
            break
        for candle in page:
            by_ts[int(candle["ts"])] = candle
        oldest = min(int(c["ts"]) for c in page)
        if previous_oldest is not None and oldest >= previous_oldest:
            raise RuntimeError(
                f"historical pagination made no progress: oldest={oldest} previous={previous_oldest}"
            )
        previous_oldest = oldest
        cursor_end = oldest - 1
        await asyncio.sleep(0.03)

    result = sorted(by_ts.values(), key=lambda c: c["ts"])[-limit:]
    if not result:
        return []
    integrity = _historical_integrity(result, interval)
    if not integrity["ok"]:
        raise RuntimeError(f"historical integrity failed: {integrity}")
    return result


_core._freeze_nexus_clock = _freeze_full_clock
_core.fetch_history = _fetch_history_contiguous

run_real_replay = _core.run_real_replay
replay_symbol = _core.replay_symbol
fetch_history_contiguous = _fetch_history_contiguous


def main() -> int:
    return _core.main()


if __name__ == "__main__":
    raise SystemExit(main())
