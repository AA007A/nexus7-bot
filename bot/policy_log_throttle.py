"""Narrow log throttling for intentionally advisory operator drawdown messages.

This module changes observability only. It never changes risk values, return
values, engine activity, entry permissions, sizing, or exchange behavior.
"""
from __future__ import annotations

import threading
import time


class DrawdownAdvisoryLogProxy:
    def __init__(self, delegate, interval_s: float = 300.0):
        self._delegate = delegate
        self._interval_s = max(1.0, float(interval_s))
        self._last = {}
        self._lock = threading.Lock()

    @staticmethod
    def _is_drawdown_advisory(message) -> bool:
        text = str(message or "")
        return "[DRAWDOWN_ADVISORY" in text

    def warning(self, message, *args, **kwargs):
        if not self._is_drawdown_advisory(message):
            return self._delegate.warning(message, *args, **kwargs)
        # Key by format string so INSTANCE/ENGINE/V3/base advisories remain
        # individually visible while repetitive scan-loop copies are suppressed.
        key = str(message)
        now = time.monotonic()
        with self._lock:
            previous = self._last.get(key, 0.0)
            if now - previous < self._interval_s:
                return None
            self._last[key] = now
        return self._delegate.warning(message, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._delegate, name)


def wrap(log, interval_s: float = 300.0):
    if isinstance(log, DrawdownAdvisoryLogProxy):
        return log
    return DrawdownAdvisoryLogProxy(log, interval_s=interval_s)
