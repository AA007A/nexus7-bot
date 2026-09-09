"""Read-only SHADOW position forensics.

Adds structured diagnostics to KuCoinClient.get_positions() without changing
position state or invoking any exchange mutation. Intended for pre-pilot
forensic inspection while VALIDATION_LOCK is active.
"""


def install(kucoin_module, log):
    cls = kucoin_module.KuCoinClient
    if getattr(cls, "_shadow_position_forensics_installed", False):
        return

    original = cls.get_positions

    async def get_positions_with_forensics(self, *args, **kwargs):
        positions = await original(self, *args, **kwargs)
        if not getattr(self, "_shadow_readonly_active", False):
            return positions

        cache = getattr(self, "_shadow_forensics_last_by_symbol", {})
        if not isinstance(cache, dict):
            cache = {}
        seen_symbols = set()

        for p in positions or []:
            try:
                symbol = str(p.get("symbol", ""))
                side = str(p.get("side", ""))
                size = float(p.get("size", 0) or 0)
                entry = float(p.get("entryPrice", 0) or 0)
                mark = float(p.get("markPrice", 0) or 0)
                liq = float(p.get("liquidationPrice", 0) or 0)
                lev = float(p.get("leverage", 0) or 0)
                upnl = float(p.get("unrealisedPnl", 0) or 0)
                margin = float(p.get("posMargin", 0) or 0)
                sl = float(p.get("stopLoss", 0) or 0)
                tp = float(p.get("takeProfit", 0) or 0)
                seen_symbols.add(symbol)

                dist_liq_pct = None
                if mark > 0 and liq > 0:
                    if side == "Buy":
                        dist_liq_pct = (mark - liq) / mark * 100.0
                    elif side == "Sell":
                        dist_liq_pct = (liq - mark) / mark * 100.0

                # Cache structural risk state per symbol. Mark price, uPnL,
                # exchange-reported leverage and position margin are excluded:
                # production evidence shows they can fluctuate with market or
                # account state even when size/entry/liquidation/protection are
                # unchanged. Dynamic fields remain visible in emitted snapshots.
                key = (
                    side,
                    round(size, 8),
                    round(entry, 8),
                    round(liq, 8),
                    round(sl, 8),
                    round(tp, 8),
                )
                if cache.get(symbol) == key:
                    continue
                cache[symbol] = key

                log.warning(
                    "[SHADOW_POSITION_FORENSICS] symbol=%s side=%s contracts=%s "
                    "entry=%.8f mark=%.8f liquidation=%.8f leverage=%.4fx "
                    "unrealised_pnl=%.6f position_margin=%.6f stop_loss=%.8f "
                    "take_profit=%.8f distance_to_liquidation_pct=%s "
                    "read_only=true execution_effect=NONE",
                    symbol, side, size, entry, mark, liq, lev, upnl, margin,
                    sl, tp,
                    "NA" if dist_liq_pct is None else f"{dist_liq_pct:.4f}",
                )
            except Exception as exc:
                log.error(
                    "[SHADOW_POSITION_FORENSICS] diagnostic failure=%s "
                    "read_only=true execution_effect=NONE",
                    type(exc).__name__,
                )

        # Closed symbols must not leave stale cache entries; if a position is
        # opened again later, its first read must produce a fresh snapshot.
        for symbol in list(cache):
            if symbol not in seen_symbols:
                cache.pop(symbol, None)
        self._shadow_forensics_last_by_symbol = cache
        return positions

    cls.get_positions = get_positions_with_forensics
    cls._shadow_position_forensics_installed = True
    log.info(
        "[SHADOW_POSITION_FORENSICS] installed: normalized position details "
        "logged read-only under VALIDATION_LOCK"
    )
