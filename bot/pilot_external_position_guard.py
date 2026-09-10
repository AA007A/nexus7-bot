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
- pilot LIVE never auto-adopts, modifies, closes or protects unexpected exchange
  positions.
- an UNPROTECTED unexpected position blocks new pilot entries fail-closed.
- a PROTECTED unexpected position does not globally block the pilot; it remains
  external/read-only and consumes one concurrency slot through
  ``pilot_exposure_capacity``.
- symbol-specific reconciliation after an ambiguous bot submission remains
  allowed because that path is tied to the bot's own in-flight order.

No execution permission is granted by this module.
"""

import math


def _positive_stop(row) -> bool:
    """Return True only for an explicit, finite, positive exchange stop."""
    if not isinstance(row, dict):
        return False
    raw = row.get("stopLoss", row.get("stop_loss", 0))
    try:
        if isinstance(raw, bool):
            return False
        value = float(raw or 0)
    except (TypeError, ValueError):
        return False
    return math.isfinite(value) and value > 0


def install(TradingEngine, log):
    if getattr(TradingEngine, "_pilot_external_position_guard_patched", False):
        return

    # Read-only forensic logging is installed through this already-loaded
    # hardening module so SHADOW can expose normalized position details without
    # adding any exchange mutation capability.
    from bot import kucoin as _kucoin
    from bot import shadow_position_forensics as _shadow_position_forensics
    _shadow_position_forensics.install(_kucoin, log)

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

    async def _unexpected_positions(engine):
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
        unexpected = {}
        for row in rows or []:
            try:
                size = abs(float(row.get("size", 0) or 0))
            except (AttributeError, TypeError, ValueError):
                size = 0.0
            sym = str(row.get("symbol", "") or "") if isinstance(row, dict) else ""
            if size > 0 and sym and sym not in local:
                unexpected[sym] = bool(unexpected.get(sym, False) or _positive_stop(row))

        unprotected = sorted(sym for sym, protected in unexpected.items() if not protected)
        protected = sorted(sym for sym, is_protected in unexpected.items() if is_protected)

        unprotected_set = getattr(engine, "_unprotected_symbols", None)
        if isinstance(unprotected_set, set):
            unprotected_set.update(unprotected)
            # Clear stale guard-only state once the same external position has
            # an exchange-confirmed stop. IntegrityGuard remains the source of
            # truth and can re-add a symbol if another protection fault exists.
            for sym in protected:
                unprotected_set.discard(sym)

        blocked = bool(unprotected)
        setattr(engine, "_pilot_external_position_guard_blocked", blocked)

        if unprotected:
            log.critical(
                "[PILOT_EXTERNAL_POSITION_GUARD] result=BLOCKED "
                "reason=unexpected_unprotected_position symbols=%s "
                "action=no_adopt_no_mutation",
                ",".join(unprotected),
            )
        if protected:
            log.warning(
                "[PILOT_EXTERNAL_POSITION_GUARD] result=PASS_WITH_EXTERNAL_PROTECTED "
                "symbols=%s action=no_adopt_no_mutation capacity_effect=count_slot",
                ",".join(protected),
            )

        return {
            "all": sorted(unexpected),
            "protected": protected,
            "unprotected": unprotected,
        }

    if original_guard is not None:
        async def _guard_failclosed(self, *args, **kwargs):
            if not _pilot_live(self):
                return await original_guard(self, *args, **kwargs)
            state = await _unexpected_positions(self)
            if state is None:
                return None
            if state["all"]:
                # Never let the legacy guard auto-adopt/protect a manual
                # position. Protected externals may coexist, but stay external.
                return None
            return await original_guard(self, *args, **kwargs)
        TradingEngine._guard_naked_positions = _guard_failclosed

    if original_sync is not None:
        async def _sync_failclosed(self, *args, **kwargs):
            if not _pilot_live(self):
                return await original_sync(self, *args, **kwargs)
            state = await _unexpected_positions(self)
            if state is None or state["all"]:
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

            state = await _unexpected_positions(self)
            if state is None:
                current = getattr(self, "_unprotected_symbols", set())
                return list(current) if isinstance(current, set) else []
            if state["all"]:
                # Report only unsafe externals to callers that use this result
                # as a protection blocker. Protected externals remain visible
                # via IntegrityGuard/capacity accounting but are not adopted.
                return list(state["unprotected"])
            return await original_reconcile(
                self, only_symbol=only_symbol, *args, **kwargs
            )
        TradingEngine._reconcile_exchange_positions = _reconcile_failclosed

    TradingEngine._pilot_external_position_guard_patched = True
    log.warning(
        "[PILOT_EXTERNAL_POSITION_GUARD] installed: unexpected exchange "
        "positions are never auto-adopted; unprotected externals block; "
        "protected externals remain read-only and consume pilot capacity"
    )
