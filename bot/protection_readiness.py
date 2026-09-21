"""Canonical readiness derived from read-only exchange protection evidence."""
from __future__ import annotations

from bot.conditional_stop_protection import conditional_stop_confirmed
from bot.logger import log


PROTECTION_READINESS_MUST_BE_DERIVED_FROM_EXCHANGE_STATE = True


def reset_protection_readiness(engine) -> None:
    engine._protection_system_ready = False
    engine._protection_readiness_receipt = None


def _live_positions(payload) -> list[dict]:
    if not isinstance(payload, list):
        raise ValueError("position response is not a list")
    live = []
    seen = set()
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("position response contains a non-object item")
        try:
            size = abs(float(item.get("size", 0) or 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("position size is not numeric") from exc
        if size <= 0:
            continue
        symbol = str(item.get("symbol", "") or "")
        if not symbol or symbol in seen:
            raise ValueError("live position identity is missing or duplicated")
        seen.add(symbol)
        live.append(item)
    return live


def publish_exchange_protection_state(
    engine,
    positions: list[dict],
    readbacks: dict[str, tuple[bool, str]],
) -> bool:
    """Publish readiness only when every live position has a readback result."""
    live = _live_positions(positions)
    symbols = {str(position["symbol"]) for position in live}
    if set(readbacks) != symbols:
        reset_protection_readiness(engine)
        return False

    protected = set()
    unprotected = set()
    evidence = {}
    for symbol in sorted(symbols):
        result = readbacks.get(symbol)
        if not isinstance(result, tuple) or len(result) != 2:
            reset_protection_readiness(engine)
            return False
        confirmed, source = result
        source = str(source or "")
        if not source:
            reset_protection_readiness(engine)
            return False
        evidence[symbol] = source
        if confirmed is True:
            protected.add(symbol)
        else:
            unprotected.add(symbol)

    engine._protection_readiness_receipt = {
        "observed_symbols": sorted(symbols),
        "positions": len(symbols),
        "protected_positions": len(protected),
        "unprotected_positions": len(unprotected),
        "readback_complete": True,
        "evidence": evidence,
    }
    engine._protection_system_ready = bool(
        not unprotected and len(protected) == len(symbols)
    )
    log_method = log.info if engine._protection_system_ready else log.warning
    log_method(
        "[PROTECTION_READINESS] ready=%s positions=%s protected=%s "
        "unprotected=%s readback_complete=true",
        str(engine._protection_system_ready).lower(),
        len(symbols),
        len(protected),
        len(unprotected),
    )
    return engine._protection_system_ready


def protection_system_ready(engine) -> bool:
    """Evaluate the last complete readback against current runtime exposure."""
    if getattr(engine, "_protection_system_ready", False) is not True:
        return False
    receipt = getattr(engine, "_protection_readiness_receipt", None)
    if not isinstance(receipt, dict) or receipt.get("readback_complete") is not True:
        return False

    try:
        observed = set(receipt.get("observed_symbols") or ())
        current = set(getattr(engine, "positions", {}) or {})
        current.update(
            set(getattr(engine, "_external_position_symbols", set()) or set())
        )
        positions = int(receipt.get("positions", -1))
        protected = int(receipt.get("protected_positions", -1))
        unprotected = int(receipt.get("unprotected_positions", -1))
        known_unprotected = set(
            getattr(engine, "_unprotected_symbols", set()) or set()
        )
    except (TypeError, ValueError):
        return False
    if observed != current:
        return False
    if known_unprotected:
        return False

    if positions != len(observed) or unprotected != 0:
        return False
    if positions == 0:
        return protected == 0
    return protected == positions


async def refresh_protection_readiness(engine, *, positions=None) -> bool:
    """Refresh all protection evidence without mutating exchange state."""
    reset_protection_readiness(engine)
    try:
        raw = await engine.client.get_positions() if positions is None else positions
        live = _live_positions(raw)
        readbacks = {}
        for position in live:
            symbol = str(position["symbol"])
            readbacks[symbol] = await conditional_stop_confirmed(
                engine.client, position
            )
        return publish_exchange_protection_state(engine, live, readbacks)
    except Exception as exc:
        reset_protection_readiness(engine)
        log.warning(
            "[PROTECTION_READINESS] ready=false readback_complete=false reason=%s",
            type(exc).__name__,
        )
        return False
