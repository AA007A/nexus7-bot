"""PAPER-only runtime integration for NEXUS confidence outcome linkage.

This module wraps persistence functions after they have already succeeded. It is
observational only: it never changes a NEXUS decision, never affects trade
persistence success/failure, and never calls exchange endpoints.

LIVE linkage is intentionally not enabled here because real fills may shift the
persisted entry/stop geometry after execution. LIVE should later propagate an
explicit evidence_id through the core instead of using fuzzy matching.
"""
from __future__ import annotations

import time

from bot.confidence_outcome_linking import link_trade, record_trade_outcome
from bot.oos_calibration_readiness import maybe_log_readiness


def _direction(side: str, direction: str = "") -> str:
    value = str(direction or side or "").upper()
    if value in ("BUY", "LONG"):
        return "LONG"
    if value in ("SELL", "SHORT"):
        return "SHORT"
    return value


async def _link_paper_open(db, *, trade_id: int, symbol: str, side: str,
                           entry: float, stop_loss: float, direction: str,
                           log) -> None:
    try:
        evidence_id = await link_trade(
            db,
            trade_id=int(trade_id),
            symbol=str(symbol),
            side=_direction(side, direction),
            entry=float(entry),
            stop_loss=float(stop_loss),
            opened_ts=time.time(),
            mode="PAPER",
        )
        log.info(
            "[CONFIDENCE_OUTCOME_LINK] mode=PAPER trade_id=%s evidence_id=%s "
            "decision_effect=NONE execution_effect=NONE",
            trade_id, evidence_id,
        )
    except Exception as exc:
        log.warning(
            "[CONFIDENCE_OUTCOME_LINK] mode=PAPER open_link_failed trade_id=%s error=%s "
            "decision_effect=NONE execution_effect=NONE",
            trade_id, type(exc).__name__,
        )


async def _record_paper_close(db, *, trade_id: int, log) -> None:
    try:
        row = await db._fetchone(
            "SELECT r_multiple FROM trades WHERE id=? AND status='closed'",
            (int(trade_id),),
        )
        if not row or row[0] is None:
            log.info(
                "[CONFIDENCE_OUTCOME_LINK] mode=PAPER trade_id=%s outcome=UNAVAILABLE "
                "decision_effect=NONE execution_effect=NONE",
                trade_id,
            )
            return
        ok = await record_trade_outcome(
            db,
            trade_id=int(trade_id),
            r_multiple=float(row[0]),
            outcome_ts=time.time(),
        )
        log.info(
            "[CONFIDENCE_OUTCOME_LINK] mode=PAPER trade_id=%s outcome_r=%s persisted=%s "
            "decision_effect=NONE execution_effect=NONE",
            trade_id, float(row[0]), ok,
        )
        # The OOS report runs only after the realized R outcome has been
        # persisted. It is throttled and analytical-only; it never mutates
        # confidence, thresholds, risk, release state or exchange behavior.
        await maybe_log_readiness(db, log)
    except Exception as exc:
        log.warning(
            "[CONFIDENCE_OUTCOME_LINK] mode=PAPER close_link_failed trade_id=%s error=%s "
            "decision_effect=NONE execution_effect=NONE",
            trade_id, type(exc).__name__,
        )


def install(db, log) -> None:
    """Install PAPER-only post-persistence hooks exactly once."""
    if getattr(db, "_confidence_outcome_runtime_installed", False):
        return

    original_open = db.save_paper_open_atomic
    original_close = db.save_paper_close_atomic

    async def save_paper_open_atomic_wrapped(
        symbol, side, entry, size, leverage, score, state_key, state_factory,
        strategy="MTF", score_features=None, sl=0, direction="",
    ):
        trade_id = await original_open(
            symbol, side, entry, size, leverage, score, state_key, state_factory,
            strategy=strategy, score_features=score_features, sl=sl,
            direction=direction,
        )
        try:
            await _link_paper_open(
                db,
                trade_id=trade_id,
                symbol=symbol,
                side=side,
                entry=entry,
                stop_loss=sl,
                direction=direction,
                log=log,
            )
        except Exception as exc:
            log.warning(
                "[CONFIDENCE_OUTCOME_LINK] mode=PAPER open_hook_failed trade_id=%s error=%s "
                "decision_effect=NONE execution_effect=NONE",
                trade_id, type(exc).__name__,
            )
        return trade_id

    async def save_paper_close_atomic_wrapped(
        trade_id, exit_price, pnl, fees, duration_min, exit_reason,
        state_key, state_value,
    ):
        result = await original_close(
            trade_id, exit_price, pnl, fees, duration_min, exit_reason,
            state_key, state_value,
        )
        try:
            await _record_paper_close(db, trade_id=trade_id, log=log)
        except Exception as exc:
            log.warning(
                "[CONFIDENCE_OUTCOME_LINK] mode=PAPER close_hook_failed trade_id=%s error=%s "
                "decision_effect=NONE execution_effect=NONE",
                trade_id, type(exc).__name__,
            )
        return result

    db.save_paper_open_atomic = save_paper_open_atomic_wrapped
    db.save_paper_close_atomic = save_paper_close_atomic_wrapped
    db._confidence_outcome_runtime_installed = True
    log.info(
        "[CONFIDENCE_OUTCOME_LINK] PAPER runtime hooks installed; LIVE/SHADOW unchanged; "
        "decision_effect=NONE execution_effect=NONE"
    )
