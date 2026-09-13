"""Fail-closed viability verification for LIVE/PAPER scans.

This hardening closes the historical `_filter_viable_symbols` exception path
that could mark every configured symbol viable after an unexpected error.
It does not change score thresholds, leverage, sizing or order permissions.
"""
from __future__ import annotations

import math


def _positive(value) -> bool:
    try:
        x = float(value or 0)
        return math.isfinite(x) and x > 0
    except (TypeError, ValueError):
        return False


def install(TradingEngine, cfg, log) -> None:
    original = TradingEngine._ensure_viable_symbols

    async def _ensure_viable_symbols_fail_closed(self):
        try:
            ok = await original(self)
        except Exception as exc:
            self.viable_symbols = []
            log.critical(
                "[MARKET_VIABILITY_FAIL_CLOSED] original_exception=%s viable_symbols=0 entries_blocked=true",
                type(exc).__name__,
            )
            return False

        if not ok:
            self.viable_symbols = []
            return False

        instruments = self.instruments or {}
        candidates = list(self.viable_symbols or [])
        verified = []

        # Independent read. If REST is unavailable, an already-populated WS
        # ticker cache may satisfy price readiness; otherwise fail closed.
        price_map = {}
        try:
            rows = await self.client.get_all_tickers()
            for row in rows or []:
                sym = row.get("symbol")
                if sym:
                    price_map[sym] = row.get("lastPrice")
        except Exception as exc:
            log.warning(
                "[MARKET_VIABILITY_FAIL_CLOSED] ticker_refresh_failed=%s using_ws_cache_only=true",
                type(exc).__name__,
            )

        for sym in candidates:
            info = instruments.get(sym) or {}
            if not info:
                continue
            mult = info.get("multiplier")
            min_qty = info.get("minQty", 1)
            if not (_positive(mult) and _positive(min_qty)):
                continue
            price = price_map.get(sym)
            if not _positive(price):
                try:
                    price = (self.client.get_cached_ticker(sym) or {}).get("lastPrice")
                except Exception:
                    price = None
            if not _positive(price):
                continue
            verified.append(sym)

        # Never broaden the configured universe and never turn an unknown state
        # into permission to trade.
        configured = set(getattr(cfg, "SYMBOLS", []) or [])
        verified = [s for s in verified if s in configured]
        if not verified:
            self.viable_symbols = []
            log.critical(
                "[MARKET_VIABILITY_FAIL_CLOSED] verified=0 candidates=%s entries_blocked=true execution_effect=NONE",
                len(candidates),
            )
            return False

        dropped = sorted(set(candidates) - set(verified))
        self.viable_symbols = verified
        log.info(
            "[MARKET_VIABILITY_FAIL_CLOSED] verified=%s dropped=%s entries_blocked=%s execution_effect=NONE",
            len(verified),
            ",".join(dropped) if dropped else "NONE",
            "false",
        )
        return True

    TradingEngine._ensure_viable_symbols = _ensure_viable_symbols_fail_closed
    log.info(
        "[MARKET_VIABILITY_FAIL_CLOSED] installed=true thresholds_unchanged=true leverage_unchanged=true"
    )
