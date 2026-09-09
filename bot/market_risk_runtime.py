"""Optional live collectors for NEXUS market-risk intelligence.

Commercial providers are opt-in through environment API keys. Provider outage,
plan restriction, rate limit or malformed data is fail-neutral: no risk signal
is fabricated and existing execution gates are never weakened.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import aiohttp

from bot.market_risk_intelligence import assess_market_risk, compact_risk_log

_SIGNAL_TTL_S = {
    "liquidation_usd_1h": 1200.0,
    "open_interest_change_pct": 1800.0,
    "funding_rate_pct": 1800.0,
    "btc_exchange_netflow_usd": 21600.0,
    "btc_exchange_reserve_change_pct": 21600.0,
    "spx_change_pct": 900.0,
    "vix_change_pct": 900.0,
    "macro_event_severity": 1800.0,
}
_state: dict[str, Any] = {
    "signals": {},
    "signal_updated_at": {},
    "providers": {},
}
_previous_coinglass_oi: tuple[float, float] | None = None


def snapshot(now: float | None = None) -> dict[str, Any]:
    current = time.time() if now is None else float(now)
    raw = dict(_state.get("signals", {}) or {})
    timestamps = dict(_state.get("signal_updated_at", {}) or {})
    signals: dict[str, Any] = {}
    ages: dict[str, float] = {}
    for key, value in raw.items():
        ts = float(timestamps.get(key, 0) or 0)
        ttl = float(_SIGNAL_TTL_S.get(key, 1800.0))
        age = current - ts if ts > 0 else float("inf")
        if ts > 0 and -5.0 <= age <= ttl:
            signals[key] = value
            ages[key] = max(0.0, age)
    assessment = assess_market_risk(signals)
    return {
        "fresh": bool(signals),
        "signals": signals,
        "signal_ages_s": ages,
        "providers": dict(_state.get("providers", {}) or {}),
        "assessment": assessment,
    }


def _mark_provider(name: str, status: str) -> None:
    _state.setdefault("providers", {})[name] = status


def _merge_signals(values: dict[str, Any], now: float | None = None) -> None:
    clean = {k: v for k, v in values.items() if v is not None}
    if not clean:
        return
    ts = time.time() if now is None else float(now)
    _state.setdefault("signals", {}).update(clean)
    signal_ts = _state.setdefault("signal_updated_at", {})
    for key in clean:
        signal_ts[key] = ts


def parse_coinglass_liquidation(payload: dict[str, Any]) -> dict[str, float]:
    if str(payload.get("code", "")) != "0":
        return {}
    for row in payload.get("data") or []:
        if str((row or {}).get("exchange", "")).casefold() == "all":
            try:
                return {"liquidation_usd_1h": max(0.0, float(row.get("liquidation_usd", 0) or 0))}
            except (TypeError, ValueError, OverflowError):
                return {}
    return {}


def parse_coinglass_markets(payload: dict[str, Any], now: float | None = None) -> dict[str, float]:
    global _previous_coinglass_oi
    if str(payload.get("code", "")) != "0":
        return {}
    btc = next((r for r in payload.get("data") or [] if str((r or {}).get("symbol", "")).upper() == "BTC"), None)
    if not btc:
        return {}
    out: dict[str, float] = {}
    try:
        out["funding_rate_pct"] = float(btc.get("avg_funding_rate_by_oi", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        out.pop("funding_rate_pct", None)
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


def parse_cryptoquant_reserve(payload: dict[str, Any]) -> dict[str, float]:
    try:
        rows = list(((payload.get("result") or {}).get("data") or []))
        if len(rows) < 2:
            return {}
        if all(isinstance(row, dict) and row.get("date") for row in rows):
            rows.sort(key=lambda row: str(row.get("date")))
        latest = float(rows[-1].get("reserve_usd", 0) or 0)
        previous = float(rows[-2].get("reserve_usd", 0) or 0)
        if latest <= 0 or previous <= 0:
            return {}
        return {"btc_exchange_reserve_change_pct": ((latest / previous) - 1.0) * 100.0}
    except (TypeError, ValueError, OverflowError, AttributeError):
        return {}


async def _coinglass_loop(log) -> None:
    key = os.environ.get("COINGLASS_API_KEY", "").strip()
    if not key:
        _mark_provider("coinglass", "disabled_no_key")
        return
    poll_s = max(60.0, float(os.environ.get("COINGLASS_POLL_SECONDS", "14400") or 14400))
    headers = {"CG-API-KEY": key, "accept": "application/json"}
    urls = (
        "https://open-api-v4.coinglass.com/api/futures/liquidation/exchange-list?symbol=BTC&range=1h",
        "https://open-api-v4.coinglass.com/api/futures/coins-markets?per_page=20&page=1",
    )
    while True:
        try:
            statuses: list[int] = []
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(urls[0], timeout=aiohttp.ClientTimeout(total=10)) as r:
                    statuses.append(r.status)
                    if r.status == 200:
                        _merge_signals(parse_coinglass_liquidation(await r.json(content_type=None)))
                async with session.get(urls[1], timeout=aiohttp.ClientTimeout(total=10)) as r:
                    statuses.append(r.status)
                    if r.status == 200:
                        _merge_signals(parse_coinglass_markets(await r.json(content_type=None)))
            _mark_provider("coinglass", "ok" if all(s == 200 for s in statuses) else f"http_{statuses}_fail_neutral")
        except Exception as exc:
            _mark_provider("coinglass", f"unavailable:{type(exc).__name__}")
            log.warning("[MARKET_RISK_SOURCE] provider=coinglass unavailable=%s fail_neutral=true", type(exc).__name__)
        await asyncio.sleep(poll_s)


async def _cryptoquant_loop(log) -> None:
    key = os.environ.get("CRYPTOQUANT_API_KEY", "").strip()
    if not key:
        _mark_provider("cryptoquant", "disabled_no_key")
        return
    poll_s = max(300.0, float(os.environ.get("CRYPTOQUANT_POLL_SECONDS", "900") or 900))
    url = "https://api.cryptoquant.com/v1/btc/exchange-flows/reserve?exchange=all_exchange&window=day&limit=2"
    headers = {"Authorization": f"Bearer {key}", "accept": "application/json"}
    while True:
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        _merge_signals(parse_cryptoquant_reserve(await r.json(content_type=None)))
                        _mark_provider("cryptoquant", "ok")
                    else:
                        _mark_provider("cryptoquant", f"http_{r.status}_fail_neutral")
        except Exception as exc:
            _mark_provider("cryptoquant", f"unavailable:{type(exc).__name__}")
            log.warning("[MARKET_RISK_SOURCE] provider=cryptoquant unavailable=%s fail_neutral=true", type(exc).__name__)
        await asyncio.sleep(poll_s)


async def market_risk_reader_loop(log) -> None:
    log.info(
        "[MARKET_RISK_SOURCES] enabled optional providers=CoinGlass,CryptoQuant; "
        "missing_keys=fail_neutral execution_effect=NONE"
    )
    tasks = [
        asyncio.create_task(_coinglass_loop(log)),
        asyncio.create_task(_cryptoquant_loop(log)),
    ]
    try:
        while True:
            snap = snapshot()
            log.info(
                "%s providers=%s fresh_signals=%s execution_effect=NONE",
                compact_risk_log(snap["assessment"]),
                snap["providers"],
                sorted(snap["signals"]),
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

    def evaluate_with_market_risk(self, engine, client, symbol, ai_decision=None):
        reasons = list(original_evaluate(self, engine, client, symbol, ai_decision))
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
    log.warning(
        "[MARKET_RISK_INTELLIGENCE] installed: extreme combined derivatives/on-chain/macro risk "
        "blocks pilot entries; per-signal freshness enforced; provider absence fail-neutral; sizing unchanged"
    )
