"""Public Binance USD-M derivatives fallback for market-risk telemetry.

This module fills only missing/stale market-risk derivative fields. CoinGlass
remains the preferred primary source when its signals are fresh and valid.
The fallback never authorizes an order; it only supplies optional evidence to
the existing deterministic market-risk model.
"""
from __future__ import annotations

import asyncio
import math
import os
import time
from typing import Any

import aiohttp


_PROVIDER = "binance_usdm_public_fallback"
_POLL_S = max(
    60.0,
    float(os.environ.get("MARKET_RISK_BINANCE_POLL_SECONDS", "300") or 300),
)
_TIMEOUT_S = max(
    2.0,
    float(os.environ.get("MARKET_RISK_BINANCE_TIMEOUT_SECONDS", "8") or 8),
)
_MAX_FUTURE_SKEW_S = 30.0


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _ms_timestamp(value: Any) -> float | None:
    number = _finite(value)
    if number is None or number <= 0:
        return None
    if number > 1e12:
        number /= 1000.0
    if number > time.time() + _MAX_FUTURE_SKEW_S:
        return None
    return number


def parse_premium_index(
    payload: Any,
) -> tuple[dict[str, float], dict[str, float]]:
    """Return BTC funding rate as percent and exchange observation time."""
    if not isinstance(payload, dict):
        return {}, {}
    funding = _finite(payload.get("lastFundingRate"))
    observed = _ms_timestamp(payload.get("time"))
    if funding is None or observed is None:
        return {}, {}
    return (
        {"funding_rate_pct": funding * 100.0},
        {"funding_rate_pct": observed},
    )


def parse_open_interest_history(
    payload: Any,
) -> tuple[dict[str, float], dict[str, float]]:
    """Return 1h BTC open-interest value change using two Binance snapshots."""
    if not isinstance(payload, list) or len(payload) < 2:
        return {}, {}
    rows = [row for row in payload if isinstance(row, dict)]
    if len(rows) < 2:
        return {}, {}
    rows.sort(key=lambda row: _finite(row.get("timestamp")) or 0.0)
    previous, latest = rows[-2], rows[-1]
    previous_value = _finite(previous.get("sumOpenInterestValue"))
    latest_value = _finite(latest.get("sumOpenInterestValue"))
    observed = _ms_timestamp(latest.get("timestamp"))
    if (
        previous_value is None
        or latest_value is None
        or previous_value <= 0
        or latest_value < 0
        or observed is None
    ):
        return {}, {}
    change_pct = ((latest_value / previous_value) - 1.0) * 100.0
    return (
        {"open_interest_change_pct": change_pct},
        {"open_interest_change_pct": observed},
    )


async def _fetch_json(
    session: aiohttp.ClientSession,
    url: str,
) -> tuple[int, Any]:
    async with session.get(
        url,
        timeout=aiohttp.ClientTimeout(total=_TIMEOUT_S),
        headers={"User-Agent": "BGX-Capital/12.1"},
    ) as response:
        if response.status != 200:
            return response.status, None
        return response.status, await response.json(content_type=None)


def _field_needs_fallback(runtime, field: str) -> bool:
    return field not in runtime.snapshot().get("signals", {})


async def _collect_once(runtime, log) -> None:
    urls = (
        "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT",
        "https://fapi.binance.com/futures/data/openInterestHist"
        "?symbol=BTCUSDT&period=1h&limit=2",
    )
    try:
        async with aiohttp.ClientSession() as session:
            premium_result, oi_result = await asyncio.gather(
                _fetch_json(session, urls[0]),
                _fetch_json(session, urls[1]),
            )
    except Exception as exc:
        runtime._mark_provider(
            _PROVIDER,
            f"unavailable:{type(exc).__name__}:fail_neutral",
        )
        log.warning(
            "[MARKET_RISK_SOURCE] provider=%s unavailable=%s fail_neutral=true",
            _PROVIDER,
            type(exc).__name__,
        )
        return

    premium_status, premium_payload = premium_result
    oi_status, oi_payload = oi_result
    funding_values, funding_observed = parse_premium_index(premium_payload)
    oi_values, oi_observed = parse_open_interest_history(oi_payload)

    candidate_values = {**funding_values, **oi_values}
    candidate_observed = {**funding_observed, **oi_observed}
    merged: list[str] = []
    standby: list[str] = []

    for field, value in candidate_values.items():
        if _field_needs_fallback(runtime, field):
            runtime._merge_signals(
                {field: value},
                observed_at={field: candidate_observed[field]},
            )
            merged.append(field)
        else:
            standby.append(field)

    statuses = [premium_status, oi_status]
    if merged:
        state = f"ok_fallback:{','.join(sorted(merged))}"
    elif standby:
        state = f"standby_primary_fresh:{','.join(sorted(standby))}"
    elif any(status != 200 for status in statuses):
        state = f"http_{statuses}_fail_neutral"
    else:
        state = "no_valid_signals_fail_neutral"
    runtime._mark_provider(_PROVIDER, state)
    log.info(
        "[MARKET_RISK_FALLBACK] provider=%s status=%s http=%s "
        "merged=%s standby=%s raw_payload_logged=false execution_effect=NONE",
        _PROVIDER,
        state,
        statuses,
        ",".join(sorted(merged)) or "none",
        ",".join(sorted(standby)) or "none",
    )


async def reader_loop(runtime, log) -> None:
    while True:
        await _collect_once(runtime, log)
        await asyncio.sleep(_POLL_S)


def install(scoring, runtime, log) -> None:
    if getattr(scoring, "_market_risk_binance_fallback_installed", False):
        return

    # Source-age guards also remain safe for CoinGlass because its merge path
    # records fetch time as observation time when no upstream timestamp exists.
    runtime._SOURCE_MAX_AGE_S.setdefault("funding_rate_pct", 2 * 3600.0)
    runtime._SOURCE_MAX_AGE_S.setdefault(
        "open_interest_change_pct", 2 * 3600.0
    )

    original_reader = scoring.news_reader_loop

    async def combined_reader_loop():
        await asyncio.gather(original_reader(), reader_loop(runtime, log))

    scoring.news_reader_loop = combined_reader_loop
    scoring._market_risk_binance_fallback_installed = True
    log.warning(
        "[MARKET_RISK_BINANCE_FALLBACK] installed provider=BinanceUSDM "
        "public=true fields=funding_rate_pct,open_interest_change_pct "
        "primary=CoinGlassV4 fallback_only_when_primary_missing_or_stale=true "
        "execution_effect=NONE"
    )
