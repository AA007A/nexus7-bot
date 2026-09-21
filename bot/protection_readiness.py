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

    # A stale incident may be cleared only after independent flat confirmation.
    # Position read alone is insufficient: unresolved durable submissions or
    # active entry orders can still create exposure after the snapshot.
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
            log.critical(
                "[PROTECTION_STATE_RECONCILIATION] decision=KEEP_BLOCKED "
                "reason=pending_orders_read_failed error=%s",
                type(exc).__name__,
            )
            return False
        if pending:
            engine._protection_readiness_evidence["reason"] = "flat_with_pending_durable_orders"
            return False

        raw_get = getattr(client, "_get", None)
        if not callable(raw_get):
            engine._protection_readiness_evidence["reason"] = "flat_active_orders_unconfirmed"
            return False
        try:
            payload = await raw_get("/api/v1/orders", {"status": "active"}, auth=True)
        except Exception as exc:
            engine._protection_readiness_evidence["reason"] = "flat_active_orders_read_failed"
            log.critical(
                "[PROTECTION_STATE_RECONCILIATION] decision=KEEP_BLOCKED "
                "reason=active_orders_read_failed error=%s",
                type(exc).__name__,
            )
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
        active_entry = [
            row for row in active if isinstance(row, dict)
            and row.get("reduceOnly") is not True and row.get("closeOrder") is not True
        ]
        if active_entry:
            engine._protection_readiness_evidence["reason"] = "flat_with_active_entry_orders"
            return False

        before = sorted(unprotected)
        if unprotected:
            engine._unprotected_symbols = set()
            unprotected = set()
            engine._protection_readiness_evidence["unprotected_positions"] = 0
            log.warning(
                "[PROTECTION_STATE_RECONCILIATION] exchange_position_qty=0 "
                "active_entry_orders_count=0 unprotected_before=%s "
                "unprotected_after=[] decision=CLEAR_STALE reason=confirmed_flat",
                ",".join(before),
            )
        engine._protection_system_ready = True
        engine._protection_readiness_evidence["reason"] = "confirmed_flat_no_pending_exposure"
        log.info(
            "[PROTECTION_READINESS] ready=true positions=0 unprotected_positions=0 "
            "basis=CONFIRMED_EXCHANGE_FLAT invariant=PROTECTION_READINESS_MUST_BE_DERIVED_FROM_EXCHANGE_STATE"
        )
        return True

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
