"""Canonical entrypoint for historical NEXUS replay with full clock parity.

The core replay already freezes nexus_ai.time. Production hardening also checks
freshness inside market_data_integrity, which owns a separate reference to the
stdlib time module. This wrapper freezes both clocks to the historical decision
timestamp so strict closed-candle integrity remains active without comparing
historical bars to wall-clock time.
"""
from __future__ import annotations

from bot import nexus_oos_real_replay as _core


def _freeze_full_clock(nexus_ai, decision_ts_ms: int):
    from bot import market_data_integrity

    class _Clock:
        def __enter__(self):
            self._nexus_old = nexus_ai.time.time
            self._integrity_old = market_data_integrity.time.time
            frozen = lambda: decision_ts_ms / 1000.0 + 1.0
            nexus_ai.time.time = frozen
            market_data_integrity.time.time = frozen

        def __exit__(self, exc_type, exc, tb):
            nexus_ai.time.time = self._nexus_old
            market_data_integrity.time.time = self._integrity_old

    return _Clock()


_core._freeze_nexus_clock = _freeze_full_clock

run_real_replay = _core.run_real_replay
replay_symbol = _core.replay_symbol


def main() -> int:
    return _core.main()


if __name__ == "__main__":
    raise SystemExit(main())
