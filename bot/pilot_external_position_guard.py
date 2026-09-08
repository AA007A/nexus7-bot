"""Fail-closed guard for unexpected exchange positions during REAL pilot mode.

Purpose
-------
A position that exists on the exchange but is not present in ``engine.positions``
was historically eligible for automatic adoption by ``_sync_positions`` /
``_reconcile_exchange_positions``. In pilot mode that is unsafe: an external or
manual position must never become bot-managed implicitly.

This hardening is intentionally restrictive:
- PAPER is unchanged.
- SHADOW / VALIDATION_LOCK is unchanged and remains mutation-free.
- non-pilot LIVE behavior is unchanged.
- pilot LIVE blocks automatic guard/sync/reconcile mutation when an unexpected
  exchange position exists.
- symbol-specific reconciliation after an ambiguous bot submission remains
  allowed because that path is tied to the bot's own in-flight order.

No execution permission is granted by this module.
"""


def install(TradingEngine, log):
    if getattr(TradingEngine, "_pilot_external_position_guard_patched", False):
        return

    original_guard = getattr(TradingEngine, "_guard_naked_positions", None)
    original_sync = getattr(TradingEngine, "_sync_positions", None)
    original_reconcile = getattr(TradingEngine, "_reconcile_exchange_positions", None)

    def _pilot_live(engine):
        pilot = getattr(engine, "pilot", None)
        return bool(
            pilot is not None
            and getattr(pilot, "enabled", False)
            and not getattr(engine, "paper_trade", False)
            and not getattr(engine, "_validation_safety_lock_active", False)
        )

    async def _unexpected_symbols(engine):
        try:
            rows = await engine.client.get_positions()
        except Exception as exc:
            log.critical(
                "[PILOT_EXTERNAL_POSITION_GUARD] result=BLOCKED "
                "reason=position_read_failed error=%s",
                type(exc).__name__,
            )
            setattr(engine, "_pilot_external_position_guard_blocked", True)
            return None

        local = set(getattr(engine, "positions", {}) or {})
        unexpected = []
        for row in rows or []:
            try:
                size = abs(float(row.get("size", 0) or 0))
            except (TypeError, ValueError):
                size = 0.0
            sym = str(row.get("symbol", "") or "")
            if size > 0 and sym and sym not in local:
                unexpected.append(sym)

        unexpected = sorted(set(unexpected))
        if unexpected:
            unprotected = getattr(engine, "_unprotected_symbols", None)
            if isinstance(unprotected, set):
                unprotected.update(unexpected)
            setattr(engine, "_pilot_external_position_guard_blocked", True)
            log.critical(
                "[PILOT_EXTERNAL_POSITION_GUARD] result=BLOCKED "
                "reason=unexpected_exchange_position symbols=%s "
                "action=no_adopt_no_mutation",
                ",".join(unexpected),
            )
            return unexpected

        setattr(engine, "_pilot_external_position_guard_blocked", False)
        return []

    if original_guard is not None:
        async def _guard_failclosed(self, *args, **kwargs):
            if not _pilot_live(self):
                return await original_guard(self, *args, **kwargs)
            unexpected = await _unexpected_symbols(self)
            if unexpected is None or unexpected:
                return None
            return await original_guard(self, *args, **kwargs)
        TradingEngine._guard_naked_positions = _guard_failclosed

    if original_sync is not None:
        async def _sync_failclosed(self, *args, **kwargs):
            if not _pilot_live(self):
                return await original_sync(self, *args, **kwargs)
            unexpected = await _unexpected_symbols(self)
            if unexpected is None or unexpected:
                return None
            return await original_sync(self, *args, **kwargs)
        TradingEngine._sync_positions = _sync_failclosed

    if original_reconcile is not None:
        async def _reconcile_failclosed(self, only_symbol=None, *args, **kwargs):
            if not _pilot_live(self):
                return await original_reconcile(
                    self, only_symbol=only_symbol, *args, **kwargs
                )

            # A symbol-scoped reconciliation is only used by the ambiguous-fill
            # recovery path for a submission the bot itself just attempted.
            # Preserve that safety-critical path; block broad orphan adoption.
            if only_symbol:
                return await original_reconcile(
                    self, only_symbol=only_symbol, *args, **kwargs
                )

            unexpected = await _unexpected_symbols(self)
            if unexpected is None or unexpected:
                current = getattr(self, "_unprotected_symbols", set())
                return list(current) if isinstance(current, set) else []
            return await original_reconcile(
                self, only_symbol=only_symbol, *args, **kwargs
            )
        TradingEngine._reconcile_exchange_positions = _reconcile_failclosed

    TradingEngine._pilot_external_position_guard_patched = True
    log.warning(
        "[PILOT_EXTERNAL_POSITION_GUARD] installed: unexpected exchange "
        "positions are never auto-adopted in pilot LIVE"
    )
