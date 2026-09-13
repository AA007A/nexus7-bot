"""Runtime bridge between live EXTERNAL ownership evidence and post-trade accounting.

The bridge is telemetry-only. It never adopts, closes, resizes or protects a
position and never changes an execution/risk decision. A live position is
persisted as EXTERNAL only after it remains absent from local BGX state across a
bounded observation delay and exact read-only restart ownership proof still
fails. Post-trade classification may use that durable observation only when no
BGX durable/fill evidence conflicts with it.
"""
import asyncio
import time

from bot.external_origin_observation import (
    matching_external_observation,
    record_external_position,
)
from bot.restart_ownership_recovery import prove_restart_ownership

_SETTLE_S = 2.0


def _live_row(row):
    if not isinstance(row, dict):
        return False
    try:
        return bool(str(row.get("symbol", "") or "") and abs(float(row.get("size", 0) or 0)) > 0)
    except (TypeError, ValueError):
        return False


def _candidate_key(row):
    return (
        str(row.get("symbol", "") or ""),
        str(row.get("side", "") or "").upper(),
    )


async def _observe_external_candidates(engine, log):
    if getattr(engine, "paper_trade", False):
        return
    try:
        rows = await asyncio.wait_for(engine.client.get_positions(), timeout=5)
    except Exception as exc:
        log.warning(
            "[EXTERNAL_ORIGIN_RUNTIME] result=UNCONFIRMED reason=position_read_failed "
            "error=%s decision_effect=NONE execution_effect=NONE",
            type(exc).__name__,
        )
        return

    local = set((getattr(engine, "positions", {}) or {}).keys())
    explicit = set(getattr(engine, "_external_position_symbols", set()) or set())
    pending = getattr(engine, "_external_origin_pending", None)
    if not isinstance(pending, dict):
        pending = {}
        engine._external_origin_pending = pending
    recorded = getattr(engine, "_external_origin_recorded", None)
    if not isinstance(recorded, set):
        recorded = set()
        engine._external_origin_recorded = recorded

    now = time.monotonic()
    live_keys = set()
    for row in rows or []:
        if not _live_row(row):
            continue
        sym = str(row.get("symbol", "") or "")
        key = _candidate_key(row)
        live_keys.add(key)
        if key in recorded:
            continue
        # A locally managed symbol is not external unless the ownership guard
        # has already explicitly quarantined it.
        if sym in local and sym not in explicit:
            pending.pop(key, None)
            continue

        first_seen = pending.setdefault(key, now)
        if now - first_seen < _SETTLE_S:
            continue

        try:
            proof = await asyncio.wait_for(prove_restart_ownership(engine, row), timeout=12)
        except Exception as exc:
            log.warning(
                "[EXTERNAL_ORIGIN_RUNTIME] symbol=%s result=UNCONFIRMED "
                "reason=ownership_proof_failed error=%s decision_effect=NONE execution_effect=NONE",
                sym, type(exc).__name__,
            )
            continue
        if getattr(proof, "recovered", False):
            pending.pop(key, None)
            continue

        reason = f"LIVE_GUARD_{getattr(proof, 'reason', 'OWNERSHIP_NOT_PROVEN')}"
        if await record_external_position(row, reason):
            recorded.add(key)
            pending.pop(key, None)

    for key in list(pending):
        if key not in live_keys:
            pending.pop(key, None)


async def _classify_with_live_observation(accounting, original, client, receipt, registry, row):
    origin, reason = await original(client, receipt, registry, row)
    if origin in {"BGX_CONFIRMED", "MANUAL_EXTERNAL"}:
        return origin, reason

    observation = await matching_external_observation(row)
    if observation is None:
        return origin, reason

    opening_ids = accounting._opening_fill_order_ids(receipt, row)
    durable_ids = {
        str(value.get("order_id")) for value in registry
        if isinstance(value, dict) and value.get("order_id")
    }
    # Any exact BGX-order evidence wins over the external observation for audit
    # purposes: the position lifecycle may be mixed/aggregated, so we refuse to
    # claim that the whole closed position was manual/external.
    if receipt.get("ownership") == "BGX_ORDER_IDS" or any(value in durable_ids for value in opening_ids):
        return "UNKNOWN_UNATTRIBUTED", "LIVE_EXTERNAL_OBSERVATION_CONFLICTS_WITH_BGX_ORDER_HISTORY"

    return "MANUAL_EXTERNAL", "LIVE_EXTERNAL_READ_ONLY_OBSERVATION"


def install(TradingEngine, accounting, log):
    if getattr(TradingEngine, "_external_origin_runtime_patched", False):
        return

    original_guard = getattr(TradingEngine, "_guard_naked_positions", None)
    original_classify = getattr(accounting, "_classify_origin", None)

    if original_guard is not None:
        async def _guard_with_origin_observation(self, *args, **kwargs):
            result = await original_guard(self, *args, **kwargs)
            await _observe_external_candidates(self, log)
            return result
        TradingEngine._guard_naked_positions = _guard_with_origin_observation

    if original_classify is not None:
        async def _classify_origin(client, receipt, registry, row):
            return await _classify_with_live_observation(
                accounting, original_classify, client, receipt, registry, row
            )
        accounting._classify_origin = _classify_origin

    TradingEngine._external_origin_runtime_patched = True
    log.warning(
        "[EXTERNAL_ORIGIN_RUNTIME] installed durable_live_external_observation=true "
        "settle_s=%.1f bgx_conflict=UNKNOWN_UNATTRIBUTED telemetry_only=true "
        "decision_effect=NONE execution_effect=NONE",
        _SETTLE_S,
    )
