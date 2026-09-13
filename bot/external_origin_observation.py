"""Durable telemetry for exchange positions classified EXTERNAL/read-only live.

This module never changes position ownership, orders, protection, sizing or risk
state. It only persists the fact that the live ownership guard positively
observed a real exchange position and refused to adopt it as BGX.
"""
import hashlib
import json
import time

from bot import database as db
from bot.logger import log

_KEY = "external_origin_observations_v1"
_VERSION = 1
_MAX_ROWS = 200
_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000


def _direction(row):
    side = str((row or {}).get("side", "") or "").upper()
    return {"BUY": "LONG", "SELL": "SHORT", "LONG": "LONG", "SHORT": "SHORT"}.get(side, "")


def _symbol_compatible(left, right):
    a = str(left or "")
    b = str(right or "")
    return bool(a and b and (a == b or a.removesuffix("M") == b.removesuffix("M")))


def _fingerprint(symbol, direction, observed_ms, entry_price):
    bucket = observed_ms // 300000
    raw = f"{symbol}|{direction}|{bucket}|{entry_price}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


async def record_external_position(row, reason):
    """Persist one positive EXTERNAL/read-only observation; fail-neutral telemetry."""
    if not isinstance(row, dict):
        return False
    symbol = str(row.get("symbol", "") or "")
    direction = _direction(row)
    if not symbol or not direction:
        return False
    try:
        size = abs(float(row.get("size", 0) or 0))
    except (TypeError, ValueError):
        return False
    if size <= 0:
        return False

    observed_ms = int(time.time() * 1000)
    try:
        entry_price = float(row.get("entryPrice", 0) or 0)
    except (TypeError, ValueError):
        entry_price = 0.0
    item = {
        "version": _VERSION,
        "fingerprint": _fingerprint(symbol, direction, observed_ms, entry_price),
        "symbol": symbol,
        "direction": direction,
        "observed_at_ms": observed_ms,
        "size": size,
        "size_unit": str(row.get("sizeUnit", "") or "UNKNOWN"),
        "entry_price": entry_price,
        "classification": "EXTERNAL_READ_ONLY",
        "reason": str(reason or "OWNERSHIP_NOT_PROVEN"),
        "source": "LIVE_EXTERNAL_POSITION_GUARD",
    }
    try:
        raw = await db.load_key_value(_KEY, strict=True)
        payload = json.loads(raw) if raw else {"version": _VERSION, "observations": []}
        if payload.get("version") != _VERSION or not isinstance(payload.get("observations"), list):
            raise ValueError("invalid external-origin registry")
        cutoff = observed_ms - _MAX_AGE_MS
        observations = [
            value for value in payload["observations"]
            if isinstance(value, dict) and int(value.get("observed_at_ms", 0) or 0) >= cutoff
        ]
        if any(value.get("fingerprint") == item["fingerprint"] for value in observations):
            return True
        observations.append(item)
        observations = observations[-_MAX_ROWS:]
        encoded = json.dumps({"version": _VERSION, "observations": observations}, sort_keys=True, separators=(",", ":"))
        if await db.save_key_value(_KEY, encoded, strict=True) is not True:
            raise db.PersistenceError("external-origin persistence unconfirmed")
        log.info(
            "[EXTERNAL_ORIGIN_OBSERVATION] symbol=%s direction=%s reason=%s "
            "classification=EXTERNAL_READ_ONLY durable=true bot_will_not_claim_trade=true "
            "decision_effect=NONE execution_effect=NONE",
            symbol, direction, item["reason"],
        )
        return True
    except Exception as exc:
        log.warning(
            "[EXTERNAL_ORIGIN_OBSERVATION] symbol=%s result=UNCONFIRMED error=%s "
            "decision_effect=NONE execution_effect=NONE",
            symbol, type(exc).__name__,
        )
        return False


async def matching_external_observation(row):
    """Return a durable live EXTERNAL observation overlapping one closed position row."""
    if not isinstance(row, dict):
        return None
    symbol = str(row.get("symbol", "") or "")
    direction = str(row.get("side", "") or "").upper()
    direction = {"BUY": "LONG", "SELL": "SHORT", "LONG": "LONG", "SHORT": "SHORT"}.get(direction, direction)
    try:
        opened = int(row.get("openTime", 0) or 0)
        closed = int(row.get("closeTime", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not symbol or direction not in {"LONG", "SHORT"} or not opened or not closed or closed < opened:
        return None
    try:
        raw = await db.load_key_value(_KEY, strict=True)
        if not raw:
            return None
        payload = json.loads(raw)
    except Exception:
        return None
    if payload.get("version") != _VERSION or not isinstance(payload.get("observations"), list):
        return None

    # A live observation must fall inside the exchange-reported lifecycle, with
    # only a small clock/indexing tolerance. Latest match wins.
    tolerance_ms = 120000
    matches = []
    for value in payload["observations"]:
        if not isinstance(value, dict) or value.get("classification") != "EXTERNAL_READ_ONLY":
            continue
        if not _symbol_compatible(value.get("symbol"), symbol):
            continue
        if str(value.get("direction", "")).upper() != direction:
            continue
        try:
            observed = int(value.get("observed_at_ms", 0) or 0)
        except (TypeError, ValueError):
            continue
        if opened - tolerance_ms <= observed <= closed + tolerance_ms:
            matches.append(value)
    if not matches:
        return None
    return max(matches, key=lambda value: int(value.get("observed_at_ms", 0) or 0))
