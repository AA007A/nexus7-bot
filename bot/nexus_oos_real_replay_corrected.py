"""Canonical entrypoint for historical NEXUS replay with full clock parity.

Python modules importing ``time`` share the same stdlib module object. Historical
replay therefore freezes that shared clock exactly once per synchronous NEXUS
decision and restores it exactly once afterwards. This keeps every production
freshness wrapper on the same historical timestamp without leaking the frozen
clock into subsequent candidates.
"""
from __future__ import annotations

import time as _stdlib_time

from bot import nexus_oos_real_replay as _core


def _freeze_full_clock(nexus_ai, decision_ts_ms: int):
    from bot import market_data_integrity

    class _Clock:
        def __enter__(self):
            self._old = _stdlib_time.time
            self._frozen = lambda: decision_ts_ms / 1000.0 + 1.0
            _stdlib_time.time = self._frozen
            # Explicit invariants: both production modules must observe the same
            # shared stdlib clock while the historical decision is evaluated.
            assert nexus_ai.time.time is self._frozen
            assert market_data_integrity.time.time is self._frozen
            return self

        def __exit__(self, exc_type, exc, tb):
            _stdlib_time.time = self._old
            assert nexus_ai.time.time is self._old
            assert market_data_integrity.time.time is self._old

    return _Clock()


_core._freeze_nexus_clock = _freeze_full_clock

run_real_replay = _core.run_real_replay
replay_symbol = _core.replay_symbol


def main() -> int:
    return _core.main()


if __name__ == "__main__":
    raise SystemExit(main())
