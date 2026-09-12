"""Freshness and semantic hardening for optional NEXUS market context.

This module fixes two independent evidence-quality problems without changing
execution authorization, leverage, score thresholds, sizing or drawdown rules:

1. The legacy ``market_data.update_coinglass`` path is not CoinGlass. It reads
   Binance Futures public endpoints as a proxy for long/short, taker flow and
   BTC open interest. The proxy is now named truthfully, refreshed on demand,
   timestamped per field and excluded from scoring whenever stale.
2. The active public-RSS headline classifier is made negation/context aware so
   phrases such as ``ETF not approved`` cannot become bullish merely because
   they contain words like ETF/approval, and ``not hacked`` does not become a
   bearish hack signal.

All external evidence remains optional. Read/parse failures never invent a
value: the affected evidence is simply omitted from the score.
"""
from __future__ import annotations

import asyncio
import math
import os
import re
import time
from typing import Any

import aiohttp


DERIVATIVES_PROVIDER = "BINANCE_FUTURES_PROXY"
DERIVATIVES_TTL_S = float(os.environ.get("NEXUS_DERIVATIVES_TTL_S", "360"))
DERIVATIVES_MIN_REFRESH_S = float(
    os.environ.get("NEXUS_DERIVATIVES_MIN_REFRESH_S", "60")
)
DERIVATIVES_TIMEOUT_S = float(os.environ.get("NEXUS_DERIVATIVES_TIMEOUT_S", "6"))
_MAX_FUTURE_SKEW_S = 30.0

_refresh_lock: asyncio.Lock | None = None


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _latest_source_row(rows):
    """Select newest source timestamp, never substitute fetch time for age."""
    valid = [r for r in rows if isinstance(r, dict) and _finite(r.get("timestamp")) is not None]
    return max(valid, key=lambda r: float(r["timestamp"])) if valid else {}


def _field_timestamp(cache: dict, field: str) -> float:
    timestamps = cache.get("_field_fetched_at", {})
    if not isinstance(timestamps, dict):
        return 0.0
    return float(_finite(timestamps.get(field)) or 0.0)


def _field_is_fresh(cache: dict, field: str, now: float | None = None) -> bool:
    current = time.time() if now is None else float(now)
    ts = _field_timestamp(cache, field)
    if ts <= 0:
        return False
    age = current - ts
    return -_MAX_FUTURE_SKEW_S <= age <= DERIVATIVES_TTL_S


def _ls_score(value: Any) -> int:
    ls = _finite(value)
    if ls is None:
        return 0
    if ls > 1.5:
        return 10
    if ls > 1.1:
        return 5
    if ls < 0.7:
        return -10
    if ls < 0.9:
        return -5
    return 0


def _taker_score(value: Any) -> int:
    ratio = _finite(value)
    if ratio is None:
        return 0
    if ratio > 1.3:
        return 8
    if ratio < 0.8:
        return -8
    return 0


def _derivative_components(cache: dict, now: float | None = None) -> dict:
    """Return raw vs freshness-eligible derivative score components."""
    current = time.time() if now is None else float(now)
    fields = {
        "ls_ratio": (_ls_score, cache.get("ls_ratio")),
        "taker_buy_ratio": (_taker_score, cache.get("taker_buy_ratio")),
    }
    all_score = 0
    fresh_score = 0
    fresh_fields: list[str] = []
    stale_fields: list[str] = []
    fresh_signals: list[str] = []

    for name, (scorer, value) in fields.items():
        contribution = scorer(value)
        all_score += contribution
        if _field_is_fresh(cache, name, current):
            fresh_score += contribution
            fresh_fields.append(name)
            numeric = _finite(value)
            if name == "ls_ratio" and numeric is not None:
                fresh_signals.append(
                    f"L/S={numeric:.2f}(fresh:{DERIVATIVES_PROVIDER})"
                )
            elif name == "taker_buy_ratio" and numeric is not None:
                fresh_signals.append(
                    f"TAKER={numeric:.2f}(fresh:{DERIVATIVES_PROVIDER})"
                )
        elif value is not None:
            stale_fields.append(name)

    ages = []
    for field in ("ls_ratio", "taker_buy_ratio", "btc_oi"):
        ts = _field_timestamp(cache, field)
        if ts > 0:
            ages.append(max(0.0, current - ts))

    return {
        "all_score": all_score,
        "fresh_score": fresh_score,
        "fresh_fields": fresh_fields,
        "stale_fields": stale_fields,
        "fresh_signals": fresh_signals,
        "age_s": max(ages) if ages else None,
    }


def _sentiment_label(score: float) -> str:
    if score >= 15:
        return "BULLISH"
    if score <= -15:
        return "BEARISH"
    if score >= 5:
        return "SLIGHTLY_BULLISH"
    if score <= -5:
        return "SLIGHTLY_BEARISH"
    return "NEUTRAL"


def _server_timestamp(value: Any, now: float) -> float | None:
    ts = _finite(value)
    if ts is None or ts <= 0:
        return None
    if ts > 1e12:
        ts /= 1000.0
    if ts > now + _MAX_FUTURE_SKEW_S:
        return None
    # 5m Binance buckets can legitimately be a few minutes old. Very old
    # responses are retained only as stale evidence and therefore not scored.
    return ts


async def _fetch_json(session: aiohttp.ClientSession, url: str):
    async with session.get(
        url,
        timeout=aiohttp.ClientTimeout(total=DERIVATIVES_TIMEOUT_S),
        headers={"User-Agent": "BGX-Capital/12.1"},
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP_{response.status}")
        return await response.json()


async def _refresh_derivatives_proxy(mdata, log, *, force: bool = False) -> dict:
    """Refresh Binance Futures proxy evidence and timestamp each valid field."""
    global _refresh_lock
    cache = getattr(mdata, "_coinglass_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        mdata._coinglass_cache = cache

    now = time.time()
    last_attempt = float(_finite(cache.get("_last_refresh_attempt")) or 0.0)
    if not force and now - last_attempt < DERIVATIVES_MIN_REFRESH_S:
        return _derivative_components(cache, now)

    if _refresh_lock is None:
        _refresh_lock = asyncio.Lock()

    async with _refresh_lock:
        now = time.time()
        last_attempt = float(_finite(cache.get("_last_refresh_attempt")) or 0.0)
        if not force and now - last_attempt < DERIVATIVES_MIN_REFRESH_S:
            return _derivative_components(cache, now)
        cache["_last_refresh_attempt"] = now
        cache["_provider"] = DERIVATIVES_PROVIDER
        field_ts = cache.setdefault("_field_fetched_at", {})
        if not isinstance(field_ts, dict):
            field_ts = {}
            cache["_field_fetched_at"] = field_ts

        urls = (
            "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
            "?symbol=BTCUSDT&period=5m&limit=2",
            "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT",
            "https://fapi.binance.com/futures/data/takerlongshortRatio"
            "?symbol=BTCUSDT&period=5m&limit=2",
        )

        try:
            async with aiohttp.ClientSession() as session:
                results = await asyncio.gather(
                    *[_fetch_json(session, url) for url in urls],
                    return_exceptions=True,
                )
        except Exception as exc:
            log.warning(
                "[DERIVATIVES_PROXY] provider=%s result=UNAVAILABLE error=%s "
                "stale_evidence_excluded=true",
                DERIVATIVES_PROVIDER,
                type(exc).__name__,
            )
            return _derivative_components(cache, time.time())

        updated: list[str] = []
        ls_data, oi_data, taker_data = results
        completion_ts = time.time()

        if isinstance(ls_data, list) and ls_data and isinstance(ls_data[0], dict):
            row = _latest_source_row(ls_data)
            ts = _server_timestamp(row.get("timestamp"), completion_ts)
            ratio = _finite(row.get("longShortRatio"))
            long_ratio = _finite(row.get("longAccount"))
            short_ratio = _finite(row.get("shortAccount"))
            if ts is not None and ratio is not None and ratio > 0:
                cache["ls_ratio"] = ratio
                if long_ratio is not None:
                    cache["btc_long_ratio"] = long_ratio
                if short_ratio is not None:
                    cache["btc_short_ratio"] = short_ratio
                field_ts["ls_ratio"] = ts
                updated.append("ls_ratio")

        if isinstance(oi_data, dict):
            oi = _finite(oi_data.get("openInterest"))
            if oi is not None and oi >= 0:
                cache["btc_oi"] = oi
                field_ts["btc_oi"] = completion_ts
                updated.append("btc_oi")

        if isinstance(taker_data, list) and taker_data and isinstance(taker_data[0], dict):
            row = _latest_source_row(taker_data)
            ts = _server_timestamp(row.get("timestamp"), completion_ts)
            ratio = _finite(row.get("buySellRatio"))
            buy_vol = _finite(row.get("buyVol"))
            sell_vol = _finite(row.get("sellVol"))
            if ts is not None and ratio is not None and ratio > 0:
                cache["taker_buy_ratio"] = ratio
                if buy_vol is not None:
                    cache["taker_buy_vol"] = buy_vol
                if sell_vol is not None:
                    cache["taker_sell_vol"] = sell_vol
                field_ts["taker_buy_ratio"] = ts
                updated.append("taker_buy_ratio")

        if updated:
            cache["_last_refresh_success"] = completion_ts

        snapshot = _derivative_components(cache, completion_ts)
        status = (
            "FRESH"
            if len(snapshot["fresh_fields"]) == 2
            else "PARTIAL"
            if snapshot["fresh_fields"]
            else "STALE_OR_UNAVAILABLE"
        )
        log.info(
            "[DERIVATIVES_PROXY] provider=%s result=%s updated=%s fresh=%s "
            "stale=%s ttl_s=%s",
            DERIVATIVES_PROVIDER,
            status,
            ",".join(updated) or "none",
            ",".join(snapshot["fresh_fields"]) or "none",
            ",".join(snapshot["stale_fields"]) or "none",
            int(DERIVATIVES_TTL_S),
        )
        return snapshot


_NEGATORS = {
    "not", "no", "never", "without", "false", "fake", "unconfirmed",
    "denied", "rejected", "rejects", "denies",
}

_STRONG_BULLISH = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:approved|approves|approval granted)\b.{0,40}\b(?:spot\s+)?(?:bitcoin|btc|ethereum|eth)?\s*etf\b",
        r"\b(?:spot\s+)?(?:bitcoin|btc|ethereum|eth)?\s*etf\b.{0,40}\b(?:approved|approval granted)\b",
        r"\b(?:ban|restrictions?)\s+(?:lifted|removed|ended)\b",
        r"\blawsuit\s+(?:dismissed|dropped|withdrawn)\b",
    )
)

_STRONG_BEARISH = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:spot\s+)?(?:bitcoin|btc|ethereum|eth)?\s*etf\b.{0,40}\b(?:not\s+approved|rejected|denied|delayed)\b",
        r"\b(?:not\s+approved|approval\s+(?:denied|rejected)|rejected|denied)\b.{0,40}\b(?:etf|application|proposal)\b",
        r"\b(?:etf|application|proposal)\b.{0,30}\bapproval\b.{0,15}\b(?:denied|rejected|delayed)\b",
    )
)


def _negated_before(text: str, index: int) -> bool:
    tokens = re.findall(r"[a-z]+(?:n't)?", text[max(0, index - 48):index].lower())[-5:]
    return any(token in _NEGATORS or token.endswith("n't") for token in tokens)


def _keyword_hits(text: str, keywords, *, skip: set[str] | None = None) -> float:
    lowered = text.lower()
    total = 0.0
    ignored = skip or set()
    for keyword in keywords:
        kw = str(keyword or "").strip().lower()
        if not kw or kw in ignored:
            continue
        start = 0
        while True:
            idx = lowered.find(kw, start)
            if idx < 0:
                break
            if not _negated_before(lowered, idx):
                total += 1.0
            start = idx + max(1, len(kw))
    return total


def classify_headline(text: str, scoring) -> tuple[str, float, bool]:
    """Negation/context-aware replacement for score._classify_news."""
    raw = str(text or "")
    lowered = raw.lower().replace("’", "'")
    macro_keywords = getattr(scoring, "_MACRO_KEYWORDS", ())
    bullish_keywords = getattr(scoring, "_BULLISH_KW", ())
    bearish_keywords = getattr(scoring, "_BEARISH_KW", ())
    is_fomc = any(str(keyword).lower() in lowered for keyword in macro_keywords)

    # ETF/approval are not directional in isolation; SEC is not bearish in
    # isolation. Context decides those cases below.
    bull = _keyword_hits(
        lowered,
        bullish_keywords,
        skip={"etf", "approval"},
    )
    bear = _keyword_hits(
        lowered,
        bearish_keywords,
        skip={"sec"},
    )

    strong_bull = sum(1 for pattern in _STRONG_BULLISH if pattern.search(lowered))
    strong_bear = sum(1 for pattern in _STRONG_BEARISH if pattern.search(lowered))
    bull += strong_bull * 2.5
    bear += strong_bear * 2.5

    diff = bull - bear
    total = bull + bear
    if diff > 0.5:
        confidence = min(0.95, 0.55 + min(0.30, abs(diff) * 0.10) + min(0.10, total * 0.02))
        return "BULLISH", round(confidence, 3), is_fomc
    if diff < -0.5:
        confidence = min(0.95, 0.55 + min(0.30, abs(diff) * 0.10) + min(0.10, total * 0.02))
        return "BEARISH", round(confidence, 3), is_fomc
    return "NEUTRO", 0.3, is_fomc


def install(TradingEngine, scoring, log) -> None:
    if getattr(TradingEngine, "_derivatives_news_freshness_installed", False):
        return

    from bot import market_data as mdata

    original_market_sentiment = mdata.get_market_sentiment
    original_nexus_validate = TradingEngine._nexus_validate

    async def refresh_derivatives_proxy(*, force: bool = False):
        return await _refresh_derivatives_proxy(mdata, log, force=force)

    async def legacy_update_coinglass():
        # Backwards-compatible name only. The source is explicitly Binance
        # Futures proxy data, not CoinGlass.
        return await refresh_derivatives_proxy(force=True)

    def market_sentiment_with_fresh_derivatives():
        base = original_market_sentiment()
        out = dict(base) if isinstance(base, dict) else {"score": 0, "signals": []}
        cache = getattr(mdata, "_coinglass_cache", {}) or {}
        parts = _derivative_components(cache)

        base_score = float(_finite(out.get("score")) or 0.0)
        corrected = max(
            -100.0,
            min(100.0, base_score - parts["all_score"] + parts["fresh_score"]),
        )
        signals = [
            str(item)
            for item in (out.get("signals", []) or [])
            if not str(item).startswith(("L/S=", "TAKER=", "DERIVATIVES_PROXY="))
        ]
        signals.extend(parts["fresh_signals"])
        if parts["stale_fields"]:
            signals.append(
                "DERIVATIVES_PROXY=STALE_EXCLUDED("
                + ",".join(parts["stale_fields"])
                + ")"
            )
        elif parts["fresh_fields"]:
            signals.append("DERIVATIVES_PROXY=FRESH")
        else:
            signals.append("DERIVATIVES_PROXY=UNAVAILABLE")

        out.update({
            "score": corrected,
            "sentiment": _sentiment_label(corrected),
            "signals": signals,
            "ls_ratio": (
                cache.get("ls_ratio")
                if _field_is_fresh(cache, "ls_ratio")
                else None
            ),
            "derivatives_source": DERIVATIVES_PROVIDER,
            "derivatives_fresh_fields": list(parts["fresh_fields"]),
            "derivatives_stale_fields": list(parts["stale_fields"]),
            "derivatives_age_s": (
                round(float(parts["age_s"]), 1)
                if parts["age_s"] is not None
                else None
            ),
            "derivatives_score_component": parts["fresh_score"],
        })
        return out

    async def nexus_validate_with_fresh_external_context(self, sig, *args, **kwargs):
        try:
            await refresh_derivatives_proxy(force=False)
        except Exception as exc:
            # Optional evidence failure must never become invented evidence.
            # The synchronous score wrapper excludes stale cache fields.
            log.warning(
                "[DERIVATIVES_PROXY] symbol=%s refresh_error=%s "
                "stale_evidence_excluded=true",
                getattr(sig, "symbol", "?"),
                type(exc).__name__,
            )
        return await original_nexus_validate(self, sig, *args, **kwargs)

    def classify_news_hardened(text: str):
        return classify_headline(text, scoring)

    scoring._classify_news = classify_news_hardened
    mdata.get_market_sentiment = market_sentiment_with_fresh_derivatives
    mdata.refresh_derivatives_proxy = refresh_derivatives_proxy
    mdata.update_coinglass = legacy_update_coinglass
    TradingEngine._nexus_validate = nexus_validate_with_fresh_external_context
    TradingEngine._derivatives_news_freshness_installed = True

    log.warning(
        "[EXTERNAL_CONTEXT_FRESHNESS] installed derivatives_provider=%s "
        "derivatives_ttl_s=%s refresh_min_s=%s stale_derivatives_excluded=true "
        "news_negation_aware=true thresholds_unchanged=true execution_permissions_unchanged=true",
        DERIVATIVES_PROVIDER,
        int(DERIVATIVES_TTL_S),
        int(DERIVATIVES_MIN_REFRESH_S),
    )
