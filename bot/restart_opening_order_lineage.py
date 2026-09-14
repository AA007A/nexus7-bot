"""Restore exact opening-order lineage on positions recovered after restart.

This module is accounting-only.  The authoritative ownership decision remains
``restart_ownership_recovery`` / ``pilot_external_position_guard``.  After that
existing guard has proved a position belongs to BGX, this wrapper copies the
already-proven opening ``order_id`` into ``Position._forensic_lineage`` so a
later close can be reconciled to KuCoin by exact opening-order lineage.

No order, position size, leverage, stop, target or entry authorization is
changed here.  Missing or conflicting evidence is never invented/overwritten.
"""
from __future__ import annotations


def _restore_position_lineage(engine, symbol: str, log) -> bool:
    recovered = set(getattr(engine, "_recovered_position_symbols", set()) or set())
    if symbol not in recovered:
        return False

    proofs = getattr(engine, "_restart_ownership_proofs", {}) or {}
    proof = proofs.get(symbol) if isinstance(proofs, dict) else None
    position = (getattr(engine, "positions", {}) or {}).get(symbol)
    if position is None or proof is None or not bool(getattr(proof, "recovered", False)):
        log.critical(
            "[RESTART_LINEAGE_RECOVERY] symbol=%s result=NOT_RESTORED "
            "reason=missing_recovered_position_or_proof "
            "execution_effect=ACCOUNTING_METADATA_ONLY",
            symbol,
        )
        return False

    opening_order_id = str(getattr(proof, "order_id", "") or "").strip()
    if not opening_order_id:
        log.critical(
            "[RESTART_LINEAGE_RECOVERY] symbol=%s result=NOT_RESTORED "
            "reason=missing_exact_opening_order_id invented=false "
            "execution_effect=ACCOUNTING_METADATA_ONLY",
            symbol,
        )
        return False

    raw_lineage = getattr(position, "_forensic_lineage", None)
    lineage = dict(raw_lineage) if isinstance(raw_lineage, dict) else {}
    existing = str(lineage.get("opening_order_id", "") or "").strip()
    if existing and existing != opening_order_id:
        log.critical(
            "[RESTART_LINEAGE_RECOVERY] symbol=%s result=NOT_RESTORED "
            "reason=opening_order_id_conflict invented=false overwritten=false "
            "execution_effect=ACCOUNTING_METADATA_ONLY",
            symbol,
        )
        return False

    lineage["opening_order_id"] = opening_order_id
    position._forensic_lineage = lineage
    log.warning(
        "[RESTART_LINEAGE_RECOVERY] symbol=%s result=RESTORED "
        "opening_order_id_present=true evidence=exact_durable_exchange_proof "
        "invented=false execution_effect=ACCOUNTING_METADATA_ONLY",
        symbol,
    )
    return True


def install(TradingEngine, log) -> None:
    if getattr(TradingEngine, "_restart_opening_order_lineage_patched", False):
        return

    original_load = getattr(TradingEngine, "_load_existing_positions", None)
    if original_load is None:
        raise RuntimeError("TradingEngine._load_existing_positions unavailable")

    async def _load_existing_with_lineage(self, *args, **kwargs):
        result = await original_load(self, *args, **kwargs)
        recovered = set(getattr(self, "_recovered_position_symbols", set()) or set())
        for symbol in sorted(recovered):
            _restore_position_lineage(self, symbol, log)
        return result

    TradingEngine._load_existing_positions = _load_existing_with_lineage
    TradingEngine._restart_opening_order_lineage_patched = True
    log.warning(
        "[RESTART_LINEAGE_RECOVERY] installed=true source=exact_restart_ownership_proof "
        "opening_order_id_rehydrated=true missing_or_conflicting_evidence_invented=false "
        "leverage_unchanged=true sizing_unchanged=true close_logic_unchanged=true "
        "execution_effect=ACCOUNTING_METADATA_ONLY"
    )
