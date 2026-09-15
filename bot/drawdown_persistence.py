"""Durable, cash-flow-aware account-equity high-water mark for LIVE drawdown.

Safety invariants: ordinary trading never lowers the peak; only verified external
capital flow may rebase it lower; malformed persistence fails closed; the known
2026-09-14 corrupt HWM has a narrowly evidence-bound repair; every HWM write is
paired with durable provenance. No execution authorization exists here.
"""
from __future__ import annotations

import math

from bot import database as db
from bot import hwm_provenance
from bot.logger import log

DURABLE_EQUITY_PEAK_KEY = "risk:account_equity_peak:v1"
_CACHE_ATTR = "_durable_account_equity_peak"
_INCIDENT_BAD_PEAK = 82_894_351_780.2826
_INCIDENT_LAST_GOOD_PEAK = 28.7914
_INCIDENT_BAD_PEAK_TOLERANCE = 1.0
_MAX_UNEXPLAINED_PEAK_TO_EQUITY_RATIO = 1_000.0


def _positive_finite(value, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} boolean")
    out = float(value)
    if not math.isfinite(out) or out <= 0:
        raise ValueError(f"{label} must be positive and finite")
    return out


def _matches_known_20260914_corruption(persisted: float) -> bool:
    return abs(persisted - _INCIDENT_BAD_PEAK) <= _INCIDENT_BAD_PEAK_TOLERANCE


def _validate_peak_vs_equity(peak: float, equity: float) -> None:
    ratio = peak / equity
    if not math.isfinite(ratio) or ratio > _MAX_UNEXPLAINED_PEAK_TO_EQUITY_RATIO:
        raise db.PersistenceError("durable equity peak is implausible relative to current account equity")


def _apply_peak(risk, peak: float, current_equity: float, *, allow_lower: bool = False) -> None:
    peak = _positive_finite(peak, "equity peak")
    current_equity = _positive_finite(current_equity, "account equity")
    v3 = getattr(risk, "_v3", None)
    if v3 is not None:
        if allow_lower and hasattr(v3, "rebase_peak_equity"):
            v3.rebase_peak_equity(peak)
        elif hasattr(v3, "restore_peak_equity"):
            if allow_lower and hasattr(v3, "_peak_equity"):
                v3._peak_equity = peak
            else:
                v3.restore_peak_equity(peak)
    legacy = getattr(risk, "_legacy", risk)
    if allow_lower:
        legacy.peak_balance = peak
    else:
        existing = float(getattr(legacy, "peak_balance", 0.0) or 0.0)
        legacy.peak_balance = max(existing, peak)
        peak = legacy.peak_balance
    legacy.drawdown = max(0.0, (peak - current_equity) / peak)


async def _load_peak(risk, *, strict: bool) -> tuple[float | None, str]:
    cached = getattr(risk, _CACHE_ATTR, None)
    if cached is not None:
        try:
            return _positive_finite(cached, "cached equity peak"), "cache"
        except (TypeError, ValueError) as exc:
            raise db.PersistenceError("cached durable equity peak is malformed") from exc
    raw = await db.load_key_value(DURABLE_EQUITY_PEAK_KEY, strict=strict)
    if raw is None:
        return None, "database"
    try:
        return _positive_finite(raw, "persisted equity peak"), "database"
    except (TypeError, ValueError) as exc:
        raise db.PersistenceError("durable equity peak is malformed") from exc


async def _write_peak_with_provenance(*, old_peak, new_peak, equity, reason, evidence_ref, strict):
    ok = await db.save_key_value(DURABLE_EQUITY_PEAK_KEY, format(new_peak, ".17g"), strict=strict)
    if strict and not ok:
        raise db.PersistenceError("durable equity peak write not confirmed")
    prov_ok = await hwm_provenance.persist_hwm_provenance(
        reason=reason, old_peak=old_peak, new_peak=new_peak,
        account_equity=equity, evidence_ref=evidence_ref, strict=strict,
    )
    if strict and not prov_ok:
        raise db.PersistenceError("durable equity peak provenance write not confirmed")


async def restore_update_real_account_peak(risk, equity: float, *, strict: bool = True) -> float:
    equity = _positive_finite(equity, "account equity")
    persisted, _ = await _load_peak(risk, strict=strict)
    repaired = False
    if persisted is not None:
        if _matches_known_20260914_corruption(persisted):
            old_peak = persisted
            persisted = max(_INCIDENT_LAST_GOOD_PEAK, equity)
            await _write_peak_with_provenance(
                old_peak=old_peak, new_peak=persisted, equity=equity,
                reason="incident_repair",
                evidence_ref="2026-09-14:exact_corrupt_persisted_signature+authenticated_equity",
                strict=strict,
            )
            setattr(risk, _CACHE_ATTR, persisted)
            repaired = True
            log.critical(
                "[DURABLE_DRAWDOWN_REPAIR] incident=2026-09-14-corrupt-hwm old_peak=%.4f "
                "last_good_peak=%.4f current_equity=%.4f repaired_peak=%.4f "
                "provenance=durable execution_effect=NONE",
                old_peak, _INCIDENT_LAST_GOOD_PEAK, equity, persisted,
            )
        else:
            _validate_peak_vs_equity(persisted, equity)

    peak = max(equity, persisted or equity)
    needs_write = persisted is None or peak > persisted
    if needs_write:
        await _write_peak_with_provenance(
            old_peak=persisted, new_peak=peak, equity=equity,
            reason="bootstrap" if persisted is None else "new_equity_high",
            evidence_ref="authenticated_account_equity",
            strict=strict,
        )

    setattr(risk, _CACHE_ATTR, peak)
    _apply_peak(risk, peak, equity, allow_lower=repaired)
    log.info(
        "[DURABLE_DRAWDOWN] equity=%.4f peak_equity=%.4f drawdown=%.2f%% source=%s "
        "persistence=%s provenance=%s execution_effect=NONE",
        equity, peak, max(0.0, (peak - equity) / peak) * 100.0,
        "bootstrap" if persisted is None else ("incident_repair" if repaired else "restored"),
        "repaired" if repaired else ("updated" if needs_write else "unchanged"),
        "updated" if (repaired or needs_write) else "unchanged",
    )
    return peak


async def rebase_real_account_peak_for_external_flow(
    risk, current_equity: float, *, pre_flow_equity: float, post_flow_equity: float,
    flow_type: str, flow_amount: float, flow_offset: str, strict: bool = True,
) -> float:
    current_equity = _positive_finite(current_equity, "account equity")
    pre_flow_equity = _positive_finite(pre_flow_equity, "pre-flow equity")
    post_flow_equity = _positive_finite(post_flow_equity, "post-flow equity")
    flow_amount = _positive_finite(flow_amount, "external flow amount")
    persisted, _ = await _load_peak(risk, strict=strict)
    if persisted is None:
        raise db.PersistenceError("cannot rebase missing durable equity peak")
    ratio = post_flow_equity / pre_flow_equity
    if not math.isfinite(ratio) or ratio <= 0:
        raise ValueError("external flow ratio must be positive and finite")
    rebased_peak = max(current_equity, persisted * ratio)
    await _write_peak_with_provenance(
        old_peak=persisted, new_peak=rebased_peak, equity=current_equity,
        reason="external_capital_flow_rebase",
        evidence_ref=f"exchange_ledger:{flow_type}:{flow_offset}", strict=strict,
    )
    setattr(risk, _CACHE_ATTR, rebased_peak)
    _apply_peak(risk, rebased_peak, current_equity, allow_lower=True)
    log.warning(
        "[CAPITAL_FLOW_REBASE] type=%s amount=%.4f offset=%s pre_equity=%.4f post_equity=%.4f "
        "old_peak=%.4f new_peak=%.4f drawdown=%.2f%% provenance=durable execution_effect=NONE",
        flow_type, flow_amount, flow_offset, pre_flow_equity, post_flow_equity,
        persisted, rebased_peak, max(0.0, (rebased_peak-current_equity)/rebased_peak)*100.0,
    )
    return rebased_peak
