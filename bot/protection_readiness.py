"""Read-only authority for protection-system readiness.

Readiness is derived from authenticated KuCoin position/stop state. HTTP order
acknowledgement is never evidence of protection.

Invariant: PROTECTION_READINESS_MUST_BE_DERIVED_FROM_EXCHANGE_STATE.
"""
from __future__ import annotations

import math

from bot.conditional_stop_protection import (
    _instrument_info,
    conditional_stop_confirmed,
    read_stop_orders,
)
from bot.logger import log
from bot.native_stop_repair import _matches, _same_trigger_price


def _positive(value) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def _live_positions(rows):
    live = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _positive(abs(float(row.get("size", 0) or 0))) > 0:
            live.append(row)
    return live


def _local_direction(position) -> str:
    raw = str(getattr(position, "direction", "") or "").upper()
    return raw if raw in {"LONG", "SHORT"} else ""


async def _bgx_stop_equivalent(engine, row: dict) -> tuple[bool, str]:
    symbol = str(row.get("symbol", "") or "")
    local = (getattr(engine, "positions", {}) or {}).get(symbol)
    if local is None:
        return False, "local_position_missing"

    direction = _local_direction(local)
    intended = _positive(getattr(local, "sl", 0))
    if not direction or intended <= 0:
        return False, "local_stop_intent_missing"

    info = _instrument_info(engine.client, symbol)

    inline = _positive(row.get("stopLoss", row.get("stop_loss", 0)))
    if inline > 0 and _same_trigger_price(inline, intended, info):
        return True, "inline_stop_equivalent"

    orders = await read_stop_orders(engine.client, symbol)
    if orders is None:
        return False, "stop_readback_unconfirmed"

    long = direction == "LONG"
    expected = {
        "symbol": symbol,
        "side": "sell" if long else "buy",
        "stop": "down" if long else "up",
        "stopPrice": intended,
        "stopPriceType": "MP",
    }
    if any(_matches(order, expected, row, info) for order in orders):
        return True, "conditional_stop_equivalent"
    return False, "stop_equivalence_mismatch"


async def refresh_protection_readiness(engine) -> bool:
    """Refresh canonical protection readiness without mutating exchange state."""
    engine._protection_system_ready = False
    evidence = {
        "exchange_positions_read": False,
        "positions": None,
        "unprotected_positions": None,
        "verified": {},
    }
    engine._protection_readiness_evidence = evidence

    try:
        rows = await engine.client.get_positions()
    except Exception as exc:
        log.critical(
            "[PROTECTION_READINESS] ready=false stage=position_read error=%s",
            type(exc).__name__,
        )
        return False
    if not isinstance(rows, list):
        log.critical(
            "[PROTECTION_READINESS] ready=false stage=position_read reason=malformed_payload"
        )
        return False

    try:
        live = _live_positions(rows)
    except (TypeError, ValueError):
        log.critical(
            "[PROTECTION_READINESS] ready=false stage=position_parse reason=invalid_size"
        )
        return False

    evidence["exchange_positions_read"] = True
    evidence["positions"] = len(live)
    unprotected = set(getattr(engine, "_unprotected_symbols", set()) or set())
    evidence["unprotected_positions"] = len(unprotected)

    # Explicit zero-position semantics requested by BGX-READY-003.
    if not live:
        ready = len(unprotected) == 0
        engine._protection_system_ready = ready
        log.info(
            "[PROTECTION_READINESS] ready=%s positions=0 unprotected_positions=%s "
            "basis=EXCHANGE_ZERO_POSITION_READBACK "
            "invariant=PROTECTION_READINESS_MUST_BE_DERIVED_FROM_EXCHANGE_STATE",
            str(ready).lower(), len(unprotected),
        )
        return ready

    symbols = [str(row.get("symbol", "") or "") for row in live]
    if any(not symbol for symbol in symbols) or len(symbols) != len(set(symbols)):
        log.critical(
            "[PROTECTION_READINESS] ready=false reason=invalid_or_duplicate_position_identity"
        )
        return False

    external = set(getattr(engine, "_external_position_symbols", set()) or set())
    for row in live:
        symbol = str(row.get("symbol", "") or "")
        try:
            if symbol in external:
                protected, source = await conditional_stop_confirmed(engine.client, row)
            else:
                protected, source = await _bgx_stop_equivalent(engine, row)
        except Exception as exc:
            protected, source = False, f"readback_exception:{type(exc).__name__}"

        evidence["verified"][symbol] = {
            "protected": bool(protected),
            "source": source,
            "ownership": "EXTERNAL" if symbol in external else "BGX",
        }
        if not protected:
            log.critical(
                "[PROTECTION_READINESS] ready=false symbol=%s ownership=%s reason=%s",
                symbol, "EXTERNAL" if symbol in external else "BGX", source,
            )
            return False

    engine._protection_system_ready = True
    log.info(
        "[PROTECTION_READINESS] ready=true positions=%s verified=%s "
        "basis=EXCHANGE_PROTECTION_READBACK "
        "invariant=PROTECTION_READINESS_MUST_BE_DERIVED_FROM_EXCHANGE_STATE",
        len(live), len(evidence["verified"]),
    )
    return True
