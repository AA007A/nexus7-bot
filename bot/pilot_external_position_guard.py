"""Fail-closed guard for unexpected/manual exchange positions.

Invariant
---------
Any position that exists on the real exchange but is not present in
``engine.positions`` is external/manual and therefore read-only to NEXUS-7.
The bot may observe it, verify protection and count it toward exposure, but it
must never auto-adopt, modify stops, reduce, or close it.

This invariant applies to every non-PAPER real-exchange mode, including SHADOW,
pilot LIVE and any future non-pilot LIVE path. An unprotected external position
blocks new entries fail-closed; a protected one may coexist only under the
existing exposure-capacity policy.

The only reconciliation exception is a symbol-scoped recovery immediately after
an ambiguous submission made by NEXUS itself. That path belongs to the bot's own
in-flight order, not to unsolicited/manual-position management.

No execution permission is granted by this module.
"""

from bot.conditional_stop_protection import conditional_stop_confirmed


def install(TradingEngine, log):
    if getattr(TradingEngine, "_pilot_external_position_guard_patched", False):
        return

    from bot import kucoin as _kucoin
    from bot import shadow_position_forensics as _shadow_position_forensics
    _shadow_position_forensics.install(_kucoin, log)

    original_guard = getattr(TradingEngine, "_guard_naked_positions", None)
    original_sync = getattr(TradingEngine, "_sync_positions", None)
    original_reconcile = getattr(TradingEngine, "_reconcile_exchange_positions", None)

    def _real_exchange_mode(engine):
        return not getattr(engine, "paper_trade", False)

    async def _unexpected_positions(engine):
        try:
            rows = await engine.client.get_positions()
        except Exception as exc:
            log.critical(
                "[EXTERNAL_POSITION_IMMUTABLE] result=BLOCKED "
                "reason=position_read_failed error=%s action=no_mutation",
                type(exc).__name__,
            )
            setattr(engine, "_pilot_external_position_guard_blocked", True)
            return None

        local = set(getattr(engine, "positions", {}) or {})
        unexpected = {}
        evidence = {}
        for row in rows or []:
            try:
                size = abs(float(row.get("size", 0) or 0))
            except (AttributeError, TypeError, ValueError):
                size = 0.0
            sym = str(row.get("symbol", "") or "") if isinstance(row, dict) else ""
            if size <= 0 or not sym or sym in local:
                continue

            protected, source = await conditional_stop_confirmed(engine.client, row)
            unexpected[sym] = bool(unexpected.get(sym, False) or protected)
            if protected:
                evidence[sym] = source

        unprotected = sorted(sym for sym, protected in unexpected.items() if not protected)
        protected = sorted(sym for sym, is_protected in unexpected.items() if is_protected)

        unprotected_set = getattr(engine, "_unprotected_symbols", None)
        if isinstance(unprotected_set, set):
            unprotected_set.update(unprotected)
            for sym in protected:
                unprotected_set.discard(sym)

        blocked = bool(unprotected)
        setattr(engine, "_pilot_external_position_guard_blocked", blocked)

        if unprotected:
            log.critical(
                "[EXTERNAL_POSITION_IMMUTABLE] result=BLOCKED "
                "reason=unexpected_unprotected_position symbols=%s "
                "action=read_only_no_adopt_no_stop_change_no_reduce_no_close",
                ",".join(unprotected),
            )
        if protected:
            evidence_text = ",".join(
                f"{sym}:{evidence.get(sym, 'unknown')}" for sym in protected
            )
            log.warning(
                "[EXTERNAL_POSITION_IMMUTABLE] result=PASS_WITH_EXTERNAL_PROTECTED "
                "symbols=%s protection=%s "
                "action=read_only_no_adopt_no_stop_change_no_reduce_no_close "
                "capacity_effect=count_slot",
                ",".join(protected),
                evidence_text,
            )

        return {
            "all": sorted(unexpected),
            "protected": protected,
            "unprotected": unprotected,
        }

    if original_guard is not None:
        async def _guard_failclosed(self, *args, **kwargs):
            if not _real_exchange_mode(self):
                return await original_guard(self, *args, **kwargs)
            state = await _unexpected_positions(self)
            if state is None:
                return None
            if state["all"]:
                return None
            return await original_guard(self, *args, **kwargs)
        TradingEngine._guard_naked_positions = _guard_failclosed

    if original_sync is not None:
        async def _sync_failclosed(self, *args, **kwargs):
            if not _real_exchange_mode(self):
                return await original_sync(self, *args, **kwargs)
            state = await _unexpected_positions(self)
            if state is None or state["all"]:
                return None
            return await original_sync(self, *args, **kwargs)
        TradingEngine._sync_positions = _sync_failclosed

    if original_reconcile is not None:
        async def _reconcile_failclosed(self, only_symbol=None, *args, **kwargs):
            if not _real_exchange_mode(self):
                return await original_reconcile(
                    self, only_symbol=only_symbol, *args, **kwargs
                )

            # Reserved for the bot's own ambiguous-fill recovery immediately
            # following a NEXUS submission. External-position discovery never
            # invokes the scoped form.
            if only_symbol:
                return await original_reconcile(
                    self, only_symbol=only_symbol, *args, **kwargs
                )

            state = await _unexpected_positions(self)
            if state is None:
                current = getattr(self, "_unprotected_symbols", set())
                return list(current) if isinstance(current, set) else []
            if state["all"]:
                return list(state["unprotected"])
            return await original_reconcile(
                self, only_symbol=only_symbol, *args, **kwargs
            )
        TradingEngine._reconcile_exchange_positions = _reconcile_failclosed

    TradingEngine._pilot_external_position_guard_patched = True
    log.warning(
        "[EXTERNAL_POSITION_IMMUTABLE] installed: every non-PAPER unexpected "
        "exchange position is read-only; no auto-adopt/stop-change/reduce/close; "
        "unprotected externals block and protected externals consume capacity"
    )
