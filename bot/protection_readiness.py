"""Canonical protection-system readiness derived from exchange state.

Read-only authority. It never creates, modifies, cancels, adopts or closes an
order or position. HTTP acknowledgement from a protection write is never
readiness evidence; only independently observed exchange state is.
"""
from __future__ import annotations

from bot.conditional_stop_protection import conditional_stop_confirmed
from bot.logger import log


def _positive(value):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def _inline_stop_equivalent(position) -> bool:
    stop = _positive(position.get("stopLoss", position.get("stop_loss", 0)))
    if stop <= 0:
        return False
    reference = 0.0
    for key in ("markPrice", "mark_price", "entryPrice", "avgEntryPrice"):
        reference = _positive(position.get(key))
        if reference > 0:
            break
    if reference <= 0:
        return False
    side = str(position.get("side", "") or "").strip().lower()
    if side in ("buy", "long"):
        return stop < reference
    if side in ("sell", "short"):
        return stop > reference
    return False


def _live_positions(rows):
    live = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            size = abs(float(row.get("size", 0) or 0))
        except (TypeError, ValueError):
            return None
        if size > 0:
            live.append(row)
    return live


async def refresh_protection_readiness(engine) -> bool:
    """Refresh canonical readiness and return the derived boolean."""
    engine._protection_system_ready = False
    engine._protection_readiness_evidence = {
        "positions": None,
        "unprotected_positions": None,
        "verified": {},
        "reason": "not_evaluated",
    }

    if not bool(getattr(engine, "connected", False)):
        engine._protection_readiness_evidence["reason"] = "exchange_not_connected"
        return False

    client = getattr(engine, "client", None)
    if client is None:
        engine._protection_readiness_evidence["reason"] = "exchange_client_unavailable"
        return False

    try:
        rows = await client.get_positions()
    except Exception as exc:
        engine._protection_readiness_evidence["reason"] = "positions_read_failed"
        log.critical(
            "[PROTECTION_READINESS] ready=false stage=positions_read error=%s",
            type(exc).__name__,
        )
        return False

    if not isinstance(rows, list):
        engine._protection_readiness_evidence["reason"] = "positions_payload_malformed"
        return False

    live = _live_positions(rows)
    if live is None:
        engine._protection_readiness_evidence["reason"] = "position_size_malformed"
        return False

    unprotected = set(getattr(engine, "_unprotected_symbols", set()) or set())
    engine._protection_readiness_evidence["positions"] = len(live)
    engine._protection_readiness_evidence["unprotected_positions"] = len(unprotected)

    def _canon(value):
        symbol = str(value or "")
        return symbol[:-1] if symbol.endswith("M") else symbol

    registry = getattr(engine, "orders", None)
    unresolved_reader = getattr(registry, "unreconciled_filled_orders", None)
    mark_reconciled = getattr(registry, "mark_filled_exposure_reconciled", None)
    if not callable(unresolved_reader) or not callable(mark_reconciled):
        engine._protection_readiness_evidence["reason"] = (
            "durable_fill_reconciliation_authority_unavailable"
        )
        return False

    # Exposure reconciliation is intent-sensitive. An entry fill is absorbed
    # by a live exchange position. A reduce fill is absorbed only by a residual
    # quantity lower than the durable pre-reduce quantity; a full reduce is
    # absorbed by confirmed flatness, but only after active-order truth is read.
    live_qty = {}
    for row in live:
        canon = _canon(row.get("symbol"))
        live_qty[canon] = live_qty.get(canon, 0.0) + abs(float(row.get("size", 0) or 0))

    unresolved_fills = list(unresolved_reader() or [])
    entry_unresolved = [
        order for order in unresolved_fills
        if str(getattr(order, "exposure_intent", "INCREASE")) == "INCREASE"
    ]
    reduce_unresolved = [
        order for order in unresolved_fills
        if str(getattr(order, "exposure_intent", "INCREASE")) == "REDUCE"
    ]

    reconciled_now = 0
    for order in entry_unresolved:
        if live_qty.get(_canon(getattr(order, "symbol", "")), 0.0) > 0:
            reconciled_now += mark_reconciled(order.symbol)
    for order in reduce_unresolved:
        previous = getattr(order, "previous_position_qty", None)
        current = live_qty.get(_canon(getattr(order, "symbol", "")), 0.0)
        filled = max(0.0, float(getattr(order, "filled_qty", 0.0) or 0.0))
        if previous is not None and filled > 0 and current > 0:
            expected = max(0.0, float(previous) - filled)
            tolerance = max(1e-9, abs(expected) * 1e-6)
            if abs(current - expected) <= tolerance:
                reconciled_now += mark_reconciled(order.symbol)

    if reconciled_now:
        from bot import durable_execution as durable
        if not await durable.persist_orders(
            engine, "exposure_reconciled_from_position_truth", strict=True
        ):
            engine._protection_readiness_evidence["reason"] = (
                "durable_fill_reconciliation_persist_failed"
            )
            return False

    unresolved_fills = list(unresolved_reader() or [])
    unresolved_fill_symbols = {
        _canon(getattr(order, "symbol", "")) for order in unresolved_fills
    }

    # Reconcile stale incidents per symbol. A flat position snapshot alone is
    # insufficient: durable intents, active entry orders, and local filled
    # positions awaiting exchange convergence are independent exposure evidence.
    if not live:
        registry = getattr(engine, "orders", None)
        pending_reader = getattr(registry, "pending_orders", None)
        if not callable(pending_reader):
            engine._protection_readiness_evidence["reason"] = "flat_pending_orders_unconfirmed"
            return False
        try:
            pending = list(pending_reader() or [])
        except Exception as exc:
            engine._protection_readiness_evidence["reason"] = "flat_pending_orders_read_failed"
            log.critical("[PROTECTION_STATE_RECONCILIATION] decision=KEEP_BLOCKED reason=pending_orders_read_failed error=%s", type(exc).__name__)
            return False

        raw_get = getattr(client, "_get", None)
        if not callable(raw_get):
            engine._protection_readiness_evidence["reason"] = "flat_active_orders_unconfirmed"
            return False
        try:
            payload = await raw_get("/api/v1/orders", {"status": "active"}, auth=True)
        except Exception as exc:
            engine._protection_readiness_evidence["reason"] = "flat_active_orders_read_failed"
            log.critical("[PROTECTION_STATE_RECONCILIATION] decision=KEEP_BLOCKED reason=active_orders_read_failed error=%s", type(exc).__name__)
            return False
        if isinstance(payload, dict):
            active = payload.get("items")
            if active is None and isinstance(payload.get("data"), list):
                active = payload.get("data")
        elif isinstance(payload, list):
            active = payload
        else:
            active = None
        if not isinstance(active, list):
            engine._protection_readiness_evidence["reason"] = "flat_active_orders_payload_malformed"
            return False

        pending_symbols = {_canon(getattr(order, "symbol", "")) for order in pending}
        active_entry_symbols = {
            _canon(row.get("symbol"))
            for row in active
            if isinstance(row, dict)
            and row.get("reduceOnly") is not True
            and row.get("closeOrder") is not True
        }
        local_position_symbols = {
            _canon(symbol) for symbol in (getattr(engine, "positions", {}) or {}).keys()
        }

        # A durable REDUCE/FULL_CLOSE fill can legitimately reconcile to zero.
        # Flatness alone is insufficient: require no pending durable intent and
        # no active exposure-increasing exchange order for that symbol.
        close_reconciled = 0
        for order in list(unresolved_reader() or []):
            canon = _canon(getattr(order, "symbol", ""))
            if str(getattr(order, "exposure_intent", "INCREASE")) != "REDUCE":
                continue
            previous = getattr(order, "previous_position_qty", None)
            filled = max(0.0, float(getattr(order, "filled_qty", 0.0) or 0.0))
            if (
                previous is not None
                and filled + 1e-9 >= float(previous)
                and canon not in pending_symbols
                and canon not in active_entry_symbols
            ):
                close_reconciled += mark_reconciled(order.symbol)
        if close_reconciled:
            from bot import durable_execution as durable
            if not await durable.persist_orders(
                engine, "full_close_exposure_reconciled_flat", strict=True
            ):
                engine._protection_readiness_evidence["reason"] = (
                    "durable_fill_reconciliation_persist_failed"
                )
                return False

        unresolved_fills = list(unresolved_reader() or [])
        unresolved_fill_symbols = {
            _canon(getattr(order, "symbol", "")) for order in unresolved_fills
        }

        before = set(unprotected)
        clearable = {
            symbol for symbol in before
            if _canon(symbol) not in pending_symbols
            and _canon(symbol) not in active_entry_symbols
            and _canon(symbol) not in local_position_symbols
            and _canon(symbol) not in unresolved_fill_symbols
        }
        if clearable:
            engine._unprotected_symbols = before - clearable
            unprotected = set(engine._unprotected_symbols)

        for symbol in sorted(before):
            canon = _canon(symbol)
            if symbol in clearable:
                decision, reason = "CLEAR_STALE", "confirmed_symbol_flat"
            elif canon in pending_symbols:
                decision, reason = "KEEP_BLOCKED", "pending_durable_intent"
            elif canon in active_entry_symbols:
                decision, reason = "KEEP_BLOCKED", "active_exchange_entry"
            elif canon in unresolved_fill_symbols:
                decision, reason = "KEEP_BLOCKED", "durable_fill_exposure_not_reconciled"
            else:
                decision, reason = "KEEP_BLOCKED", "local_filled_position_awaiting_exchange_convergence"
            log.warning(
                "[PROTECTION_STATE_RECONCILIATION] symbol=%s position_qty=0 "
                "exchange_position_qty=0 active_entry_orders=%s durable_pending=%s "
                "local_materialized_position=%s durable_fill_unreconciled=%s "
                "exposure_reconciliation_complete=%s unprotected_before=true "
                "unprotected_after=%s decision=%s reason=%s",
                symbol, int(canon in active_entry_symbols), int(canon in pending_symbols),
                int(canon in local_position_symbols), int(canon in unresolved_fill_symbols),
                str(canon not in unresolved_fill_symbols).lower(),
                str(symbol in unprotected).lower(), decision, reason,
            )

        ready = len(unprotected) == 0
        engine._protection_system_ready = ready
        engine._protection_readiness_evidence["unprotected_positions"] = len(unprotected)
        engine._protection_readiness_evidence["reason"] = (
            "confirmed_flat_no_pending_exposure"
            if ready else "flat_with_unresolved_exposure_evidence"
        )
        return ready

    verified = {}
    for row in live:
        symbol = str(row.get("symbol", "") or "")
        if not symbol:
            engine._protection_readiness_evidence["reason"] = "position_symbol_missing"
            return False

        try:
            protected, evidence = await conditional_stop_confirmed(client, row)
        except Exception as exc:
            engine._protection_readiness_evidence["reason"] = "protection_readback_failed"
            log.critical(
                "[PROTECTION_READINESS] ready=false symbol=%s stage=readback error=%s",
                symbol, type(exc).__name__,
            )
            return False

        if protected and evidence == "inline_stop":
            protected = _inline_stop_equivalent(row)
            if not protected:
                evidence = "inline_stop_mismatch"

        verified[symbol] = {
            "protected": bool(protected),
            "evidence": str(evidence),
        }
        if not protected:
            engine._protection_readiness_evidence["verified"] = verified
            engine._protection_readiness_evidence["reason"] = (
                f"protection_unverified:{symbol}:{evidence}"
            )
            log.critical(
                "[PROTECTION_READINESS] ready=false symbol=%s evidence=%s "
                "http_success_is_not_readback=true",
                symbol, evidence,
            )
            return False

    live_symbols = {str(row.get("symbol", "") or "") for row in live}
    unresolved_live = sorted(live_symbols.intersection(unprotected))
    if unresolved_live:
        engine._protection_readiness_evidence["verified"] = verified
        engine._protection_readiness_evidence["reason"] = "unresolved_unprotected_symbols"
        log.critical(
            "[PROTECTION_READINESS] ready=false unresolved=%s",
            ",".join(unresolved_live),
        )
        return False

    engine._protection_readiness_evidence["verified"] = verified
    engine._protection_readiness_evidence["reason"] = "all_live_positions_readback_verified"
    engine._protection_system_ready = True
    log.info(
        "[PROTECTION_READINESS] ready=true positions=%s unprotected_positions=%s "
        "basis=EXCHANGE_READBACK invariant=PROTECTION_READINESS_MUST_BE_DERIVED_FROM_EXCHANGE_STATE",
        len(live), len(unprotected),
    )
    return True
