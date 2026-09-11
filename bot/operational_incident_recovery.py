"""One-time recovery of the LIVE drawdown baseline after a verified ownership incident.

Incident 2026-09-11:
- a manual/external NEARUSDT position was incorrectly auto-adopted on startup;
- the protection guard then emergency-closed it after a legacy stop endpoint 404;
- account-equity drawdown therefore mixed NEXUS strategy performance with an
  external/manual position and an operational bot defect;
- a subsequent external TransferIn correctly preserved that contaminated
  drawdown, so the durable HWM remained unusable for strategy gating.

This migration does NOT change MAX_DRAWDOWN and does NOT authorize execution.
It can run only once, only after a read-only exposure preflight proves there are
no exchange positions or active orders, and it preserves the last clean
pre-incident NEXUS drawdown (9.10%) instead of resetting performance to 0%.
"""
from __future__ import annotations

import json
import math
import time

from bot import database as db
from bot import drawdown_persistence as drawdown

INCIDENT_ID = "2026-09-11-near-external-ownership-p0"
MARKER_KEY = f"risk:operational_incident_rebase:{INCIDENT_ID}:v1"
# Verified clean snapshot before the manual NEAR incident:
# equity=20.8133, durable peak=22.8969 -> ~9.10% strategy drawdown.
PRESERVED_STRATEGY_DRAWDOWN = 0.0910
# Never lower a healthy/near-threshold HWM by accident. The contaminated state
# was >30%; requiring >=20% makes the migration narrowly incident-specific.
MIN_CONTAMINATED_DRAWDOWN = 0.20


def _target_peak(current_equity: float) -> float:
    equity = float(current_equity)
    if not math.isfinite(equity) or equity <= 0:
        raise ValueError("current_equity must be positive and finite")
    if not 0 <= PRESERVED_STRATEGY_DRAWDOWN < 1:
        raise RuntimeError("invalid preserved drawdown")
    return equity / (1.0 - PRESERVED_STRATEGY_DRAWDOWN)


async def maybe_rebase(engine, current_equity: float, log, *, preflight_clear: bool) -> bool:
    """Apply the incident rebase at most once and only from a flat account.

    Returns True only when the durable HWM was actually rebased.
    Any persistence/read failure raises so the caller remains fail-closed.
    """
    if getattr(engine, "paper_trade", False):
        return False
    if not preflight_clear:
        log.warning(
            "[OPERATIONAL_INCIDENT_REBASE] incident=%s result=SKIP "
            "reason=preflight_not_clear execution_effect=NONE",
            INCIDENT_ID,
        )
        return False

    marker = await db.load_key_value(MARKER_KEY, strict=True)
    if marker is not None:
        log.info(
            "[OPERATIONAL_INCIDENT_REBASE] incident=%s result=ALREADY_APPLIED "
            "execution_effect=NONE",
            INCIDENT_ID,
        )
        return False

    equity = float(current_equity)
    persisted_peak, _ = await drawdown._load_peak(engine.risk, strict=True)
    if persisted_peak is None:
        raise db.PersistenceError("incident rebase requires existing durable peak")

    contaminated_dd = max(0.0, (persisted_peak - equity) / persisted_peak)
    if contaminated_dd < MIN_CONTAMINATED_DRAWDOWN:
        # Do not mark applied: an unexpected state should remain visible for
        # investigation rather than silently consuming the migration.
        log.warning(
            "[OPERATIONAL_INCIDENT_REBASE] incident=%s result=SKIP "
            "reason=drawdown_not_incident_shaped current_dd=%.2f%% "
            "required_min=%.2f%% execution_effect=NONE",
            INCIDENT_ID,
            contaminated_dd * 100.0,
            MIN_CONTAMINATED_DRAWDOWN * 100.0,
        )
        return False

    target_peak = _target_peak(equity)
    if target_peak >= persisted_peak:
        log.warning(
            "[OPERATIONAL_INCIDENT_REBASE] incident=%s result=SKIP "
            "reason=target_does_not_lower_peak current_peak=%.4f target_peak=%.4f "
            "execution_effect=NONE",
            INCIDENT_ID, persisted_peak, target_peak,
        )
        return False

    ok = await db.save_key_value(
        drawdown.DURABLE_EQUITY_PEAK_KEY,
        format(target_peak, ".17g"),
        strict=True,
    )
    if not ok:
        raise db.PersistenceError("incident HWM rebase write not confirmed")

    setattr(engine.risk, drawdown._CACHE_ATTR, target_peak)
    drawdown._apply_peak(
        engine.risk,
        target_peak,
        equity,
        allow_lower=True,
    )

    marker_payload = json.dumps({
        "incident_id": INCIDENT_ID,
        "applied_at": time.time(),
        "equity": equity,
        "old_peak": persisted_peak,
        "new_peak": target_peak,
        "old_drawdown": contaminated_dd,
        "preserved_strategy_drawdown": PRESERVED_STRATEGY_DRAWDOWN,
        "reason": "external_manual_position_ownership_incident",
    }, separators=(",", ":"), sort_keys=True)
    marker_ok = await db.save_key_value(MARKER_KEY, marker_payload, strict=True)
    if not marker_ok:
        # Peak write is idempotent. Raising here keeps startup fail-closed; the
        # same target can be safely applied again if marker persistence failed.
        raise db.PersistenceError("incident rebase marker write not confirmed")

    log.critical(
        "[OPERATIONAL_INCIDENT_REBASE] incident=%s result=APPLIED "
        "equity=%.4f old_peak=%.4f old_drawdown=%.2f%% new_peak=%.4f "
        "preserved_strategy_drawdown=%.2f%% max_drawdown_unchanged=true "
        "execution_effect=NONE",
        INCIDENT_ID,
        equity,
        persisted_peak,
        contaminated_dd * 100.0,
        target_peak,
        PRESERVED_STRATEGY_DRAWDOWN * 100.0,
    )
    return True


def install(TradingEngine, log) -> None:
    """Run the one-time migration after the normal LIVE preflight is clear."""
    if getattr(TradingEngine, "_operational_incident_recovery_installed", False):
        return

    original_connect = TradingEngine._connect

    async def _connect_with_incident_recovery(self, *args, **kwargs):
        result = await original_connect(self, *args, **kwargs)
        if getattr(self, "paper_trade", False) or not getattr(self, "connected", False):
            return result

        preflight_clear = bool(getattr(self, "_pilot_live_prelive_ready", False))
        if not preflight_clear:
            return result

        try:
            equity = float(getattr(self, "_pilot_account_equity", 0.0) or 0.0)
            applied = await maybe_rebase(
                self,
                equity,
                log,
                preflight_clear=preflight_clear,
            )
            if applied:
                self.risk.balance_confirmed = True
        except Exception as exc:
            self._pilot_live_prelive_ready = False
            self.risk.balance_confirmed = False
            log.critical(
                "[OPERATIONAL_INCIDENT_REBASE] incident=%s result=BLOCKED "
                "reason=%s action=no_new_entry",
                INCIDENT_ID,
                type(exc).__name__,
            )
        return result

    TradingEngine._connect = _connect_with_incident_recovery
    TradingEngine._operational_incident_recovery_installed = True
    log.warning(
        "[OPERATIONAL_INCIDENT_REBASE] installed incident=%s "
        "one_time=true max_drawdown_unchanged=true",
        INCIDENT_ID,
    )
