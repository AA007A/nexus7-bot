"""Deduplicate equivalent NEXUS observability without changing trading decisions.

The engine still evaluates NEXUS on every candidate and every production gate remains
unchanged. This module only prevents the same signal state inside the same 15-minute
candle from inflating persistent decision metrics or repeatedly notifying Telegram.
"""
import logging
import re
import threading
import time

_LOCK = threading.Lock()
_SEEN = {"persistence": {}, "approved_telegram": {}}
_COUNTS = {"persistence_suppressed": 0, "approved_telegram_suppressed": 0}
_CANDLE_SECONDS = 15 * 60


def _bucket(now=None):
    return int((time.time() if now is None else now) // _CANDLE_SECONDS)


def _round_price(value):
    try:
        return round(float(value), 8)
    except (TypeError, ValueError):
        return None


def signal_key(sig, now=None):
    """Stable key for an equivalent strategy signal within one 15m candle."""
    return (
        str(getattr(sig, "symbol", "")),
        str(getattr(sig, "direction", "")),
        _round_price(getattr(sig, "entry", None)),
        _round_price(getattr(sig, "sl", None)),
        _round_price(getattr(sig, "tp", None)),
        _bucket(now),
    )


def decision_dict_key(d, now=None):
    """Equivalent approved-decision key used only for Telegram dedupe."""
    return (
        str(d.get("symbol", "")),
        str(d.get("decision", "")),
        _round_price(d.get("entry")),
        _round_price(d.get("stop_loss")),
        _round_price(d.get("take_profit")),
        _bucket(now),
    )


def _claim(channel, key):
    with _LOCK:
        seen = _SEEN[channel]
        current_bucket = key[-1]
        # Bound memory: only current/previous candle can matter.
        stale = [k for k in seen if k[-1] < current_bucket - 1]
        for k in stale:
            seen.pop(k, None)
        if key in seen:
            return False
        seen[key] = time.time()
        return True


def snapshot():
    with _LOCK:
        return dict(_COUNTS)


class _AIDecisionExactDuplicateFilter(logging.Filter):
    """Suppress only byte-equivalent AI_DECISION logs apart from ts=... .

    Different score/confidence/reason values remain visible. This is intentionally
    narrower than persistence dedupe so diagnostics are never hidden.
    """
    _ts = re.compile(r"\s+ts=\d+")

    def __init__(self):
        super().__init__()
        self._last = {}
        self._lock = threading.Lock()

    def filter(self, record):
        try:
            msg = record.getMessage()
            if not msg.startswith("[AI_DECISION]"):
                return True
            normalized = self._ts.sub("", msg)
            key = (normalized, _bucket())
            with self._lock:
                stale = [k for k in self._last if k[1] < key[1] - 1]
                for k in stale:
                    self._last.pop(k, None)
                if key in self._last:
                    return False
                self._last[key] = time.time()
        except Exception:
            return True
        return True


def install(log):
    """Patch persistence/notification observability only; execution path is untouched."""
    from bot import nexus_persistence as np
    from bot import notifier
    from bot import engine as engine_module

    if getattr(np, "_equivalent_decision_dedupe_patched", False):
        return

    original_record = np.record_decision

    async def record_decision_deduped(sig, dec):
        key = signal_key(sig)
        if not _claim("persistence", key):
            with _LOCK:
                _COUNTS["persistence_suppressed"] += 1
            return None
        return await original_record(sig, dec)

    np.record_decision = record_decision_deduped
    np._equivalent_decision_dedupe_patched = True

    original_notify_nexus = notifier.notify_nexus

    async def notify_nexus_deduped(d, approved):
        if approved:
            key = decision_dict_key(d)
            if not _claim("approved_telegram", key):
                with _LOCK:
                    _COUNTS["approved_telegram_suppressed"] += 1
                return False
        return await original_notify_nexus(d, approved)

    notifier.notify_nexus = notify_nexus_deduped
    # engine.py imports notify_nexus directly, so update that bound module global too.
    engine_module.notify_nexus = notify_nexus_deduped

    try:
        for handler in getattr(log, "handlers", []):
            if isinstance(handler, logging.StreamHandler):
                handler.addFilter(_AIDecisionExactDuplicateFilter())
    except Exception as exc:
        log.debug(
            "[NEXUS_DEDUPE] log filter installation failed: %s: %s",
            type(exc).__name__, exc,
        )

    log.info(
        "[NEXUS_DEDUPE] equivalent 15m signal observability dedupe enabled; "
        "NEXUS evaluation and all trading gates unchanged"
    )
