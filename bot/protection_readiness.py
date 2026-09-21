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

    # Explicit flat-account rule: no open exposure and no unresolved protection
    # incident is sufficient. This is not a default; it is exchange-derived.
    if not live:
        ready = len(unprotected) == 0
        engine._protection_system_ready = ready
        engine._protection_readiness_evidence["reason"] = (
            "flat_account_no_unprotected_positions"
            if ready else "flat_account_with_unresolved_unprotected_state"
        )
        log.info(
            "[PROTECTION_READINESS] ready=%s positions=0 unprotected_positions=%s "
            "basis=EXCHANGE_STATE invariant=PROTECTION_READINESS_MUST_BE_DERIVED_FROM_EXCHANGE_STATE",
            str(ready).lower(), len(unprotected),
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
