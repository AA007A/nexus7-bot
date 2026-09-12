"""Live market-risk collectors for NEXUS-7.

CoinGlass v4 is optional and API-body validated before any derivative signal is
accepted. Cross-asset macro prices come from Yahoo Finance daily chart data and
US 2Y/10Y yield changes come from the official U.S. Treasury par-yield feed.

External observations have two freshness clocks where the upstream source
publishes a timestamp: fetch freshness and source-observation freshness. A
successful HTTP fetch never refreshes an already-stale market observation.

All external sources are fail-neutral and can never authorize execution.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
import time
import xml.etree.ElementTree as ET
from typing import Any

import aiohttp

from bot.market_risk_intelligence import assess_market_risk, compact_risk_log


_SIGNAL_TTL_S = {
    "liquidation_usd_1h": 1200.0,
    "open_interest_change_pct": 1800.0,
    "funding_rate_pct": 1800.0,
    "spx_change_pct": 1800.0,
    "ndx_change_pct": 1800.0,
    "vix_change_pct": 1800.0,
    "dxy_change_pct": 1800.0,
    "us2y_yield_change_bps": 21600.0,
    "us10y_yield_change_bps": 21600.0,
    "macro_event_severity": 1800.0,
}

# Fetching the same daily close/yield repeatedly must not make that observation
# "new". These source-age limits are intentionally wider than the fetch TTL to
# tolerate ordinary market closures while still expiring genuinely stale data.
_SOURCE_MAX_AGE_S = {
    "spx_change_pct": 36 * 3600.0,
    "ndx_change_pct": 36 * 3600.0,
    "vix_change_pct": 36 * 3600.0,
    "dxy_change_pct": 36 * 3600.0,
    "us2y_yield_change_bps": 96 * 3600.0,
    "us10y_yield_change_bps": 96 * 3600.0,
}

_COINGLASS_DEFAULT_POLL_S = 900.0
_MAX_FUTURE_SKEW_S = 30.0

_state: dict[str, Any] = {
    "signals": {},
    "signal_updated_at": {},
    "signal_observed_at": {},
    "signal_providers": {},
    "providers": {},
}
_previous_coinglass_oi: tuple[float, float] | None = None


def snapshot(now: float | None = None) -> dict[str, Any]:
    current = time.time() if now is None else float(now)
    raw = dict(_state.get("signals", {}) or {})
    fetched_at = dict(_state.get("signal_updated_at", {}) or {})
    observed_at = dict(_state.get("signal_observed_at", {}) or {})
    signals: dict[str, Any] = {}
    fetch_ages: dict[str, float] = {}
    source_ages: dict[str, float] = {}
    stale_reasons: dict[str, str] = {}

    for key, value in raw.items():
        fetch_ts = float(fetched_at.get(key, 0) or 0)
        source_ts = float(observed_at.get(key, fetch_ts) or 0)
        ttl = float(_SIGNAL_TTL_S.get(key, 1800.0))
        fetch_age = current - fetch_ts if fetch_ts > 0 else float("inf")
        source_age = current - source_ts if source_ts > 0 else float("inf")
        source_max_age = _SOURCE_MAX_AGE_S.get(key)

        if fetch_ts <= 0 or not (-_MAX_FUTURE_SKEW_S <= fetch_age <= ttl):
            stale_reasons[key] = "FETCH_TTL"
            continue
        if source_max_age is not None:
            if source_ts <= 0 or not (
                -_MAX_FUTURE_SKEW_S <= source_age <= float(source_max_age)
            ):
                stale_reasons[key] = "SOURCE_AGE"
                continue

        signals[key] = value
        fetch_ages[key] = max(0.0, fetch_age)
        source_ages[key] = max(0.0, source_age)

    assessment = assess_market_risk(signals)
    return {
        "fresh": bool(signals),
        "signals": signals,
        "signal_providers": {key: (_state.get("signal_providers", {}) or {}).get(key, "unknown") for key in signals},
        "signal_ages_s": fetch_ages,
        "signal_source_ages_s": source_ages,
        "stale_reasons": stale_reasons,
        "providers": dict(_state.get("providers", {}) or {}),
        "assessment": assessment,
    }


def _mark_provider(name: str, status: str) -> None:
    _state.setdefault("providers", {})[name] = status


def _merge_signals(
    values: dict[str, Any],
    now: float | None = None,
    observed_at: float | dict[str, float] | None = None,
    provider: str = "unknown",
) -> None:
    clean = {k: v for k, v in values.items() if v is not None}
    if not clean:
        return
    ts = time.time() if now is None else float(now)
    _state.setdefault("signals", {}).update(clean)
    signal_ts = _state.setdefault("signal_updated_at", {})
    source_ts = _state.setdefault("signal_observed_at", {})
    owners = _state.setdefault("signal_providers", {})
    for key in clean:
        owners[key] = provider
        signal_ts[key] = ts
        if isinstance(observed_at, dict):
            source_ts[key] = float(observed_at.get(key, ts) or ts)
        elif observed_at is None:
            source_ts[key] = ts
        else:
            source_ts[key] = float(observed_at)


def _coinglass_api_code(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "invalid_payload"
    return str(payload.get("code", ""))


def _finite_number(value: Any) -> float | None:
    """Normalize optional numeric evidence without silent exception swallowing."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _coinglass_provider_status(
    http_statuses: list[int],
    api_codes: list[str],
    signal_names: list[str],
) -> str:
    if (
        http_statuses
        and all(status == 200 for status in http_statuses)
        and api_codes
        and all(code == "0" for code in api_codes)
        and signal_names
    ):
        return f"ok:{len(set(signal_names))}_signals"
    if signal_names:
        return (
            f"partial:{len(set(signal_names))}_signals:"
            f"http={http_statuses}:api={api_codes}"
        )
    return f"no_valid_signals:http={http_statuses}:api={api_codes}:fail_neutral"


def parse_coinglass_liquidation(payload: dict[str, Any]) -> dict[str, float]:
    if _coinglass_api_code(payload) != "0":
        return {}
    for row in payload.get("data") or []:
        if str((row or {}).get("exchange", "")).casefold() == "all":
            try:
                return {
                    "liquidation_usd_1h": max(
                        0.0, float(row.get("liquidation_usd", 0) or 0)
                    )
                }
            except (TypeError, ValueError, OverflowError):
                return {}
    return {}


def parse_coinglass_markets(
    payload: dict[str, Any], now: float | None = None
) -> dict[str, float]:
    global _previous_coinglass_oi
    if _coinglass_api_code(payload) != "0":
        return {}
    btc = next(
        (
            row
            for row in payload.get("data") or []
            if str((row or {}).get("symbol", "")).upper() == "BTC"
        ),
        None,
    )
    if not btc:
        return {}

    out: dict[str, float] = {}
    funding = _finite_number(btc.get("avg_funding_rate_by_oi"))
    if funding is not None:
        out["funding_rate_pct"] = funding

    try:
        oi = float(btc.get("open_interest_usd", 0) or 0)
        ts = time.time() if now is None else float(now)
        prev = _previous_coinglass_oi
        if oi > 0 and prev and prev[0] > 0 and ts > prev[1]:
            out["open_interest_change_pct"] = ((oi / prev[0]) - 1.0) * 100.0
        if oi > 0:
            _previous_coinglass_oi = (oi, ts)
    except (TypeError, ValueError, OverflowError):
        out.pop("open_interest_change_pct", None)
    return out


def parse_chart_change_with_observation(
    payload: dict[str, Any],
) -> tuple[float | None, float | None]:
    try:
        result = payload["chart"]["result"][0]
        closes_raw = result["indicators"]["quote"][0]["close"]
        timestamps_raw = result.get("timestamp") or []
        pairs: list[tuple[float | None, float]] = []
        for index, raw_close in enumerate(closes_raw):
            if raw_close is None:
                continue
            close = float(raw_close)
            ts: float | None = None
            if index < len(timestamps_raw) and timestamps_raw[index] is not None:
                ts = float(timestamps_raw[index])
            pairs.append((ts, close))
        if len(pairs) < 2 or pairs[-2][1] == 0:
            return None, None
        change = ((pairs[-1][1] / pairs[-2][1]) - 1.0) * 100.0
        return change, pairs[-1][0]
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return None, None


def parse_chart_change(payload: dict[str, Any]) -> float | None:
    value, _ = parse_chart_change_with_observation(payload)
    return value


def _parse_utc_timestamp(value: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def parse_treasury_curve_xml_with_observation(
    xml_text: str,
) -> tuple[dict[str, float], float | None]:
    """Return latest 2Y/10Y daily changes plus the Treasury observation time."""
    namespaces = {
        "a": "http://www.w3.org/2005/Atom",
        "d": "http://schemas.microsoft.com/ado/2007/08/dataservices",
        "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
    }
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}, None

    rows: list[tuple[str, float, float]] = []
    for entry in root.findall("a:entry", namespaces):
        props = entry.find("a:content/m:properties", namespaces)
        if props is None:
            continue
        date_node = props.find("d:NEW_DATE", namespaces)
        y2_node = props.find("d:BC_2YEAR", namespaces)
        y10_node = props.find("d:BC_10YEAR", namespaces)
        if (
            date_node is None
            or y2_node is None
            or y10_node is None
            or not date_node.text
            or not y2_node.text
            or not y10_node.text
        ):
            continue
        try:
            rows.append((date_node.text, float(y2_node.text), float(y10_node.text)))
        except (TypeError, ValueError, OverflowError):
            continue

    if len(rows) < 2:
        return {}, None
    rows.sort(key=lambda row: row[0])
    previous = rows[-2]
    latest = rows[-1]
    values = {
        "us2y_yield_change_bps": (latest[1] - previous[1]) * 100.0,
        "us10y_yield_change_bps": (latest[2] - previous[2]) * 100.0,
    }
    return values, _parse_utc_timestamp(latest[0])


def parse_treasury_curve_xml(xml_text: str) -> dict[str, float]:
    values, _ = parse_treasury_curve_xml_with_observation(xml_text)
    return values


async def _coinglass_loop(log) -> None:
    key = os.environ.get("COINGLASS_API_KEY", "").strip()
    if not key:
        _mark_provider("coinglass_v4", "disabled_no_key")
        return
    poll_s = max(
        60.0,
        float(
            os.environ.get(
                "COINGLASS_POLL_SECONDS", str(int(_COINGLASS_DEFAULT_POLL_S))
            )
            or _COINGLASS_DEFAULT_POLL_S
        ),
    )
    headers = {"CG-API-KEY": key, "accept": "application/json"}
    urls = (
        "https://open-api-v4.coinglass.com/api/futures/liquidation/"
        "exchange-list?symbol=BTC&range=1h",
        "https://open-api-v4.coinglass.com/api/futures/coins-markets"
        "?per_page=20&page=1",
    )
    while True:
        try:
            statuses: list[int] = []
            api_codes: list[str] = []
            merged_names: list[str] = []
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(
                    urls[0], timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    statuses.append(response.status)
                    if response.status == 200:
                        payload = await response.json(content_type=None)
                        api_codes.append(_coinglass_api_code(payload))
                        values = parse_coinglass_liquidation(payload)
                        _merge_signals(values, provider="coinglass_v4")
                        merged_names.extend(values)
                    else:
                        api_codes.append("http_non_200")

                async with session.get(
                    urls[1], timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    statuses.append(response.status)
                    if response.status == 200:
                        payload = await response.json(content_type=None)
                        api_codes.append(_coinglass_api_code(payload))
                        values = parse_coinglass_markets(payload)
                        _merge_signals(values, provider="coinglass_v4")
                        merged_names.extend(values)
                    else:
                        api_codes.append("http_non_200")

            status = _coinglass_provider_status(
                statuses, api_codes, merged_names
            )
            _mark_provider("coinglass_v4", status)
            if not status.startswith("ok:"):
                log.warning(
                    "[MARKET_RISK_SOURCE] provider=coinglass_v4 status=%s "
                    "raw_payload_logged=false fail_neutral=true",
                    status,
                )
        except Exception as exc:
            _mark_provider("coinglass_v4", f"unavailable:{type(exc).__name__}")
            log.warning(
                "[MARKET_RISK_SOURCE] provider=coinglass_v4 unavailable=%s "
                "fail_neutral=true",
                type(exc).__name__,
            )
        await asyncio.sleep(poll_s)


async def _cross_asset_loop(log) -> None:
    symbols = {
        "spx_change_pct": "^GSPC",
        "ndx_change_pct": "^NDX",
        "vix_change_pct": "^VIX",
        "dxy_change_pct": "DX-Y.NYB",
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    while True:
        ok = 0
        stale = 0
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                for key, symbol in symbols.items():
                    url = (
                        "https://query1.finance.yahoo.com/v8/finance/chart/"
                        f"{symbol}?interval=1d&range=5d"
                    )
                    try:
                        async with session.get(
                            url, timeout=aiohttp.ClientTimeout(total=8)
                        ) as response:
                            if response.status != 200:
                                continue
                            value, observed_at = parse_chart_change_with_observation(
                                await response.json(content_type=None)
                            )
                            if value is None or observed_at is None:
                                continue
                            source_age = time.time() - observed_at
                            if not (
                                -_MAX_FUTURE_SKEW_S
                                <= source_age
                                <= _SOURCE_MAX_AGE_S[key]
                            ):
                                stale += 1
                                continue
                            _merge_signals(
                                {key: value},
                                observed_at=observed_at,
                            )
                            ok += 1
                    except Exception as exc:
                        log.debug(
                            "[CROSS_ASSET] symbol=%s unavailable=%s "
                            "fail_neutral=true",
                            symbol,
                            type(exc).__name__,
                        )
            if ok:
                _mark_provider(
                    "yahoo_finance_daily",
                    f"ok_fresh:{ok}/{len(symbols)}:stale={stale}",
                )
            elif stale:
                _mark_provider(
                    "yahoo_finance_daily",
                    f"stale_source:{stale}/{len(symbols)}:fail_neutral",
                )
            else:
                _mark_provider(
                    "yahoo_finance_daily", "unavailable_fail_neutral"
                )
        except Exception as exc:
            _mark_provider(
                "yahoo_finance_daily", f"unavailable:{type(exc).__name__}"
            )
        await asyncio.sleep(900)


async def _treasury_curve_loop(log) -> None:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "accept": "application/xml,text/xml",
    }
    while True:
        year = time.gmtime().tm_year
        url = (
            "https://home.treasury.gov/resource-center/data-chart-center/"
            "interest-rates/pages/xml?data=daily_treasury_yield_curve&"
            f"field_tdr_date_value={year}"
        )
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=12)
                ) as response:
                    if response.status == 200:
                        values, observed_at = (
                            parse_treasury_curve_xml_with_observation(
                                await response.text()
                            )
                        )
                        source_age = (
                            time.time() - observed_at
                            if observed_at is not None
                            else float("inf")
                        )
                        max_age = _SOURCE_MAX_AGE_S["us2y_yield_change_bps"]
                        if (
                            len(values) == 2
                            and observed_at is not None
                            and -_MAX_FUTURE_SKEW_S <= source_age <= max_age
                        ):
                            _merge_signals(
                                values,
                                observed_at=observed_at,
                            )
                            _mark_provider(
                                "us_treasury_curve", "ok_source_fresh"
                            )
                        elif len(values) == 2:
                            _mark_provider(
                                "us_treasury_curve",
                                "stale_source_fail_neutral",
                            )
                        else:
                            _mark_provider(
                                "us_treasury_curve",
                                "incomplete_fail_neutral",
                            )
                    else:
                        _mark_provider(
                            "us_treasury_curve",
                            f"http_{response.status}_fail_neutral",
                        )
        except Exception as exc:
            _mark_provider(
                "us_treasury_curve", f"unavailable:{type(exc).__name__}"
            )
            log.warning(
                "[MARKET_RISK_SOURCE] provider=us_treasury_curve "
                "unavailable=%s fail_neutral=true",
                type(exc).__name__,
            )
        await asyncio.sleep(900)


async def market_risk_reader_loop(log) -> None:
    log.info(
        "[MARKET_RISK_SOURCES] "
        "providers=CoinGlassV4(optional,api-body-validated),"
        "YahooFinanceDaily(SPX,NDX,VIX,DXY),"
        "US_Treasury(2Y,10Y),public_macro_news; "
        "source_timestamp_freshness=true execution_effect=NONE"
    )
    tasks = [
        asyncio.create_task(_coinglass_loop(log)),
        asyncio.create_task(_cross_asset_loop(log)),
        asyncio.create_task(_treasury_curve_loop(log)),
    ]
    try:
        while True:
            snap = snapshot()
            log.info(
                "%s providers=%s fresh_signals=%s stale=%s "
                "execution_effect=NONE",
                compact_risk_log(snap["assessment"]),
                snap["providers"],
                sorted(snap["signals"]),
                snap["stale_reasons"],
            )
            await asyncio.sleep(120)
    finally:
        for task in tasks:
            task.cancel()


def install(PilotGuard, scoring, log) -> None:
    if getattr(PilotGuard, "_market_risk_intelligence_installed", False):
        return

    original_news_loop = scoring.news_reader_loop

    async def combined_reader_loop():
        await asyncio.gather(original_news_loop(), market_risk_reader_loop(log))

    original_evaluate = PilotGuard.evaluate

    def evaluate_with_market_risk(
        self, engine, client, symbol, ai_decision=None
    ):
        reasons = list(
            original_evaluate(self, engine, client, symbol, ai_decision)
        )
        snap = snapshot()
        assessment = snap["assessment"]
        if snap["fresh"] and assessment.block_new_entries:
            reasons.append(
                f"15_MARKET_RISK: EXTREME score={assessment.score} "
                f"signals={','.join(assessment.reasons[:5]) or 'NONE'}"
            )
        self.state.blocked_reasons = reasons
        return reasons

    scoring.news_reader_loop = combined_reader_loop
    PilotGuard.evaluate = evaluate_with_market_risk
    PilotGuard._market_risk_intelligence_installed = True

    coinglass_configured = bool(
        os.environ.get("COINGLASS_API_KEY", "").strip()
    )
    log.warning(
        "[MARKET_RISK_INTELLIGENCE] installed: "
        "CoinGlassV4 configured=%s api_body_validated=true default_poll_s=%s; "
        "YahooFinanceDaily + official US Treasury use source timestamps; "
        "EXTREME combined risk blocks pilot entries; "
        "external signals never authorize execution",
        str(coinglass_configured).lower(),
        int(_COINGLASS_DEFAULT_POLL_S),
    )
