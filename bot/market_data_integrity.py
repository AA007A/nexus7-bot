"""Market-data integrity policy shared by LIVE strategy and NEXUS.

This module is intentionally fail-closed around data correctness. It addresses
four production risks discovered in the 2026-09-11 audit:

* KuCoin Classic Futures documents ``limitCandle.candles[5]`` (transaction
  volume) as incorrect. The adjacent transaction-amount field at index 6 is
  therefore used as the activity series for WS candles instead of the
  documented-bad field.
* The REST kline seed and WS updates must use the same activity unit. REST rows
  are normalized so their index 5 also carries index 6 before the legacy
  KuCoin parser puts them into the shared cache. This prevents a cache seeded
  with contract volume from being compared with WS transaction amount.
* A candle is considered closed from its timestamp and timeframe boundary, not
  from its position in a list.
* NEXUS gets an independent hard freshness/gap check so a numerically acceptable
  DataQuality score can never authorize a trade on critically stale candles.

No threshold, leverage, risk budget, release token, or exchange-order permission
is changed here.
"""
from __future__ import annotations

import copy
import math
import time
from typing import Iterable


_INTERVAL_MINUTES = {
    "1": 1, "1m": 1, "1min": 1,
    "3": 3, "3m": 3, "3min": 3,
    "5": 5, "5m": 5, "5min": 5,
    "15": 15, "15m": 15, "15min": 15,
    "30": 30, "30m": 30, "30min": 30,
    "60": 60, "1h": 60, "1hour": 60,
    "120": 120, "2h": 120, "2hour": 120,
    "240": 240, "4h": 240, "4hour": 240,
    "480": 480, "8h": 480, "8hour": 480,
    "720": 720, "12h": 720, "12hour": 720,
    "D": 1440, "1d": 1440, "1day": 1440,
    "W": 10080, "1w": 10080, "1week": 10080,
}

# Timestamps earlier than year 2000 are treated as synthetic/test placeholders,
# not as trustworthy exchange time. This preserves isolated unit fixtures while
# production KuCoin timestamps (seconds or milliseconds) remain strict.
_MIN_REAL_TS_MS = 946684800000


def interval_ms(timeframe: str) -> int:
    minutes = _INTERVAL_MINUTES.get(str(timeframe))
    if minutes is None:
        try:
            minutes = int(timeframe)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unsupported timeframe: {timeframe}") from exc
    if minutes <= 0:
        raise ValueError(f"invalid timeframe: {timeframe}")
    return int(minutes) * 60_000


def candle_ts_ms(candle: dict) -> int | None:
    try:
        raw = candle.get("ts")
        if isinstance(raw, bool):
            return None
        value = float(raw)
        if not math.isfinite(value) or value <= 0:
            return None
        ts = int(value)
        if ts < 100_000_000_000:
            ts *= 1000
        if ts < _MIN_REAL_TS_MS:
            return None
        return ts
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def has_usable_timestamps(candles: Iterable[dict] | None) -> bool:
    data = list(candles or [])
    return bool(data) and all(candle_ts_ms(c) is not None for c in data)


def _ordered_unique(candles: Iterable[dict]) -> list:
    """Return strict chronological candles, keeping the newest duplicate."""
    by_ts = {}
    for candle in candles:
        ts = candle_ts_ms(candle)
        if ts is None:
            continue
        by_ts[ts] = candle
    return [by_ts[ts] for ts in sorted(by_ts)]


def closed_candles(candles: Iterable[dict] | None, timeframe: str,
                   *, now_ms: int | None = None,
                   require_timestamps: bool = False) -> list:
    """Select bars whose timestamp + interval is at or before ``now``.

    If timestamps are synthetic/missing and strict timestamps are not required,
    the input is returned unchanged so legacy unit fixtures can still exercise
    unrelated logic. Production/NEXUS hard gates call this with
    ``require_timestamps=True``.
    """
    data = list(candles or [])
    if not data:
        return []
    if not has_usable_timestamps(data):
        return [] if require_timestamps else data

    now = int(now_ms if now_ms is not None else time.time() * 1000)
    width = interval_ms(timeframe)
    ordered = _ordered_unique(data)
    return [c for c in ordered if candle_ts_ms(c) + width <= now]


def prepare_strategy_series(candles: Iterable[dict] | None, timeframe: str,
                            minimum: int, *, now_ms: int | None = None) -> list:
    """Prepare inputs for legacy analyzers that still drop ``[-1]``.

    The real closed/open decision is made here from timestamps. A sentinel is
    appended only so the existing analyzer's historical ``[:-1]`` operation
    removes the sentinel rather than a confirmed bar. If timestamps are not
    usable (old unit fixtures), inputs are left untouched.
    """
    data = list(candles or [])
    if not has_usable_timestamps(data):
        return data

    closed = closed_candles(data, timeframe, now_ms=now_ms, require_timestamps=True)
    if len(closed) < minimum:
        return closed

    closed_ts = {candle_ts_ms(c) for c in closed}
    open_bars = [c for c in _ordered_unique(data) if candle_ts_ms(c) not in closed_ts]
    if open_bars:
        sentinel = dict(open_bars[-1])
    else:
        # No forming bar was supplied (common for REST responses immediately
        # after a boundary). Duplicate the last confirmed bar as a disposable
        # sentinel; the legacy analyzer drops it before indicator calculation.
        sentinel = dict(closed[-1])
        sentinel["_closed_bar_sentinel"] = True
    return closed + [sentinel]


def validate_nexus_candles(k15: list, k1h: list, k4h: list,
                           *, now_ms: int | None = None,
                           max_stale_intervals: float = 2.0) -> tuple[bool, str, tuple[list, list, list]]:
    """Hard fail-closed integrity check for the NEXUS decision boundary."""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    specs = (
        ("15m", k15, "15", 60),
        ("1h", k1h, "60", 40),
        ("4h", k4h, "240", 20),
    )
    views = []
    for name, raw, interval, minimum in specs:
        if not has_usable_timestamps(raw):
            return False, f"{name}:missing_or_invalid_timestamp", ([], [], [])
        view = closed_candles(raw, interval, now_ms=now, require_timestamps=True)
        if len(view) < minimum:
            return False, f"{name}:closed_history_{len(view)}<{minimum}", ([], [], [])

        width = interval_ms(interval)
        last_ts = candle_ts_ms(view[-1])
        last_close = last_ts + width
        age = max(0, now - last_close)
        if age > width * float(max_stale_intervals):
            return False, f"{name}:hard_stale_age_s={age/1000:.0f}", ([], [], [])

        # Recent discontinuities are a data-integrity failure for the highly
        # liquid futures universe used by NEXUS. A single missing bucket is
        # tolerated (delta <= 2x interval); larger holes fail closed.
        recent = view[-min(len(view), 40):]
        ts_values = [candle_ts_ms(c) for c in recent]
        for prev, cur in zip(ts_values, ts_values[1:]):
            if cur <= prev:
                return False, f"{name}:non_monotonic_timestamp", ([], [], [])
            if cur - prev > width * 2:
                return False, f"{name}:abnormal_gap_s={(cur-prev)/1000:.0f}", ([], [], [])
        views.append(view)

    return True, "ok", (views[0], views[1], views[2])


def _rewrite_kucoin_ws_kline_volume(msg: dict) -> tuple[dict | None, bool]:
    """Replace the KuCoin-documented-bad WS volume with transaction amount.

    Classic Futures ``/contractMarket/limitCandle`` explicitly documents index
    5 as incorrect. Index 6 is the transaction amount and is the only usable
    activity field in that message. Returning ``None`` means the event should be
    ignored rather than poisoning the indicator cache.
    """
    topic = str(msg.get("topic", "")) if isinstance(msg, dict) else ""
    data = msg.get("data", {}) if isinstance(msg, dict) else {}
    if "andle" not in topic or not isinstance(data, dict) or "candles" not in data:
        return msg, False

    candles = data.get("candles")
    if not isinstance(candles, (list, tuple)) or len(candles) < 7:
        return None, False
    try:
        activity = float(candles[6])
    except (TypeError, ValueError):
        return None, False
    if not math.isfinite(activity) or activity < 0:
        return None, False

    rewritten = copy.deepcopy(msg)
    rewritten["data"]["candles"][5] = rewritten["data"]["candles"][6]
    rewritten["data"]["_bgx_volume_source"] = "transaction_amount_index_6"
    return rewritten, True


def _rewrite_kucoin_rest_kline_volume(data) -> tuple[object, int, int]:
    """Normalize Classic Futures REST klines to the same activity basis as WS.

    ``get_klines`` historically maps raw index 5 into candle ``v``. Rather than
    maintaining two incompatible units in the shared cache, this function
    copies raw index 6 into index 5 before that legacy parser runs. Invalid rows
    are dropped rather than falling back to index 5, because fallback would
    reintroduce mixed-unit volume ratios.
    """
    if not isinstance(data, list):
        return data, 0, 0

    normalized = []
    rewritten = 0
    dropped = 0
    for row in data:
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            dropped += 1
            continue
        try:
            activity = float(row[6])
        except (TypeError, ValueError):
            dropped += 1
            continue
        if not math.isfinite(activity) or activity < 0:
            dropped += 1
            continue
        fixed = list(row)
        fixed[5] = row[6]
        normalized.append(fixed)
        rewritten += 1
    return normalized, rewritten, dropped


def install(KuCoinClient, Analyzer, log) -> None:
    """Install contained runtime guards without changing trading thresholds."""
    if not getattr(KuCoinClient, "_market_data_integrity_installed", False):
        original_get = KuCoinClient._get
        original_ws = KuCoinClient._handle_ws_message

        async def get_integrity(self, endpoint, *args, **kwargs):
            result = await original_get(self, endpoint, *args, **kwargs)
            if str(endpoint) != "/api/v1/kline/query":
                return result
            normalized, rewritten, dropped = _rewrite_kucoin_rest_kline_volume(result)
            if rewritten and not getattr(self, "_rest_volume_basis_logged", False):
                self._rest_volume_basis_logged = True
                log.warning(
                    "[MARKET_DATA_INTEGRITY] KuCoin Futures REST klines normalized "
                    "to transaction amount index 6 so REST seed and WS updates use "
                    "one activity basis; rows=%s",
                    rewritten,
                )
            if dropped:
                log.warning(
                    "[MARKET_DATA_INTEGRITY] KuCoin Futures REST dropped %s kline "
                    "row(s) without valid transaction amount index 6",
                    dropped,
                )
            return normalized

        async def handle_ws_integrity(self, msg: dict):
            rewritten, changed = _rewrite_kucoin_ws_kline_volume(msg)
            if rewritten is None:
                if not getattr(self, "_bad_ws_kline_schema_logged", False):
                    self._bad_ws_kline_schema_logged = True
                    log.error(
                        "[MARKET_DATA_INTEGRITY] KuCoin WS kline missing usable "
                        "transaction-amount field; event dropped; REST fallback required"
                    )
                return None
            if changed and not getattr(self, "_ws_volume_fix_logged", False):
                self._ws_volume_fix_logged = True
                log.warning(
                    "[MARKET_DATA_INTEGRITY] KuCoin Futures WS candles[5] ignored "
                    "because official schema marks it incorrect; using candles[6] "
                    "transaction amount for activity/volume indicators"
                )
            return await original_ws(self, rewritten)

        KuCoinClient._get = get_integrity
        KuCoinClient._handle_ws_message = handle_ws_integrity
        KuCoinClient._market_data_integrity_installed = True

    if not getattr(Analyzer, "_timestamp_closed_candle_integrity_installed", False):
        original_analyze = Analyzer.analyze_mtf

        def analyze_timestamp_closed(self, symbol, k15, k1h, k4h,
                                     min_score=60, fee_mult=2.0, vol_mult=1.0):
            now_ms = int(time.time() * 1000)
            p15 = prepare_strategy_series(k15, "15", 20, now_ms=now_ms)
            p1h = prepare_strategy_series(k1h, "60", 15, now_ms=now_ms)
            p4h = prepare_strategy_series(k4h, "240", 10, now_ms=now_ms)
            return original_analyze(
                self, symbol, p15, p1h, p4h,
                min_score=min_score, fee_mult=fee_mult, vol_mult=vol_mult,
            )

        Analyzer.analyze_mtf = analyze_timestamp_closed
        Analyzer._timestamp_closed_candle_integrity_installed = True

    log.warning(
        "[MARKET_DATA_INTEGRITY] installed kucoin_rest_ws_volume_basis=transaction_amount_index_6 "
        "kucoin_ws_bad_volume_field=blocked closed_candle_source=timestamp_boundary "
        "nexus_freshness=fail_closed thresholds_unchanged=true "
        "leverage_unchanged=true execution_permissions_unchanged=true"
    )