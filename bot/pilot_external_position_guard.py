"""Fail-closed guard for unexpected/manual exchange positions.

Invariant
---------
Any position that exists on the real exchange but is not explicitly created by
this running NEXUS-7 process is external/manual and therefore read-only.

Startup exchange positions are classified EXTERNAL before legacy startup sync
can auto-adopt them. This is deliberately conservative: after a process restart,
a surviving position is not mutated until ownership can be proven by a stronger
recovery mechanism. New positions opened by this process remain managed because
they are inserted into engine.positions only after a NEXUS submission/fill.

External positions may be observed, protection-checked and counted toward
exposure, but must never be auto-adopted, have stops changed, be reduced or be
closed by NEXUS-7. An unprotected external position blocks new entries; a
protected external may coexist only under the existing capacity policy.
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
    original_load = getattr(TradingEngine, "_load_existing_positions", None)

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
        explicit_external = set(getattr(engine, "_external_position_symbols", set()) or set())
        unexpected = {}
        evidence = {}
        for row in rows or []:
            try:
                size = abs(float(row.get("size", 0) or 0))
            except (AttributeError, TypeError, ValueError):
                size = 0.0
            sym = str(row.get("symbol", "") or "") if isinstance(row, dict) else ""
            if size <= 0 or not sym:
                continue
            # Explicit EXTERNAL ownership always wins over local presence. This
            # closes the startup-sync hole that caused the manual NEAR position
            # to be adopted and emergency-closed on 2026-09-11.
            if sym in local and sym not in explicit_external:
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
                ",".join(protected), evidence_text,
            )

        return {"all": sorted(unexpected), "protected": protected, "unprotected": unprotected}

    # P0 ownership boundary: the legacy startup loader previously inserted every
    # exchange position into engine.positions. That made a manual position look
    # bot-owned to every downstream lifecycle guard. Snapshot live symbols before
    # that loader runs and force them back to EXTERNAL/read-only afterwards.
    if original_load is not None:
        async def _load_existing_failclosed(self, *args, **kwargs):
            if not _real_exchange_mode(self):
                return await original_load(self, *args, **kwargs)
            try:
                rows = await self.client.get_positions()
            except Exception as exc:
                self._external_position_symbols = set()
                self._pilot_external_position_guard_blocked = True
                log.critical(
                    "[EXTERNAL_POSITION_OWNERSHIP] result=BLOCKED reason=preload_read_failed "
                    "error=%s action=no_adopt_no_mutation",
                    type(exc).__name__,
                )
                return None

            external = set()
            for row in rows or []:
                try:
                    if abs(float(row.get("size", 0) or 0)) > 0 and row.get("symbol"):
                        external.add(str(row["symbol"]))
                except (AttributeError, TypeError, ValueError):
                    continue
            self._external_position_symbols = external

            result = await original_load(self, *args, **kwargs)
            for sym in external:
                self.positions.pop(sym, None)
                self._trade_ids.pop(sym, None)
            if external:
                log.critical(
                    "[EXTERNAL_POSITION_OWNERSHIP] classified=EXTERNAL symbols=%s "
                    "action=read_only_no_adopt_no_stop_change_no_reduce_no_close",
                    ",".join(sorted(external)),
                )
            return result
        TradingEngine._load_existing_positions = _load_existing_failclosed

    if original_guard is not None:
        async def _guard_failclosed(self, *args, **kwargs):
            if not _real_exchange_mode(self):
                return await original_guard(self, *args, **kwargs)
            state = await _unexpected_positions(self)
            if state is None or state["all"]:
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
                return await original_reconcile(self, only_symbol=only_symbol, *args, **kwargs)

            # Symbol-scoped recovery is permitted only for a symbol that was not
            # classified external at startup. An external symbol can never use
            # this escape hatch.
            external = set(getattr(self, "_external_position_symbols", set()) or set())
            if only_symbol:
                if only_symbol in external:
                    log.critical(
                        "[EXTERNAL_POSITION_IMMUTABLE] symbol=%s scoped_reconcile=BLOCKED "
                        "action=no_adopt_no_mutation",
                        only_symbol,
                    )
                    return None
                return await original_reconcile(self, only_symbol=only_symbol, *args, **kwargs)

            state = await _unexpected_positions(self)
            if state is None:
                current = getattr(self, "_unprotected_symbols", set())
                return list(current) if isinstance(current, set) else []
            if state["all"]:
                return list(state["unprotected"])
            return await original_reconcile(self, only_symbol=only_symbol, *args, **kwargs)
        TradingEngine._reconcile_exchange_positions = _reconcile_failclosed

    TradingEngine._pilot_external_position_guard_patched = True
    log.warning(
        "[EXTERNAL_POSITION_IMMUTABLE] installed: startup exchange positions are "
        "explicitly EXTERNAL/read-only; no auto-adopt/stop-change/reduce/close; "
        "unprotected externals block and protected externals consume capacity"
    )
