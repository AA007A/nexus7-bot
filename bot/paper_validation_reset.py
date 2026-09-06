"""Explicit, idempotent PAPER validation-state reset.

This module exists only to recover a deliberately persisted PAPER simulation
when a completed validation run has exhausted its virtual drawdown budget.
It never touches LIVE mode, KuCoin positions, orders, balances, leverage, risk
limits, or execution gates.

A reset is applied only when PAPER_RESET_STATE_ONCE contains a non-empty request
id. The request id is persisted, making the operation idempotent across deploy
restarts. Persisted PAPER positions must be empty; otherwise the reset fails
closed instead of discarding simulated position state.
"""
from __future__ import annotations

import json
import os
import sys

_RESET_ENV = "PAPER_RESET_STATE_ONCE"
_RESET_MARKER_KEY = "paper_validation_reset_marker_v1"
_WALLET_KEY = "paper_wallet_state_v1"


def _requested_reset_id() -> str:
    return os.environ.get(_RESET_ENV, "").strip()[:160]


def _direct_observe(message: str) -> None:
    """Emit reset evidence independently of the application logger."""
    try:
        sys.stderr.write(f"[PAPER_RESET_OBSERVABILITY] {message}\n")
        sys.stderr.flush()
    except Exception:
        return


def _initial_balance(log) -> float:
    from bot.config import cfg

    raw = os.environ.get("PAPER_INITIAL_BALANCE", "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
            log.warning(
                "[PAPER_RESET] PAPER_INITIAL_BALANCE must be > 0; falling back to INITIAL_CAP"
            )
        except (TypeError, ValueError) as exc:
            log.warning(
                "[PAPER_RESET] invalid PAPER_INITIAL_BALANCE=%r (%s); falling back to INITIAL_CAP",
                raw,
                type(exc).__name__,
            )
    return max(0.0, float(getattr(cfg, "INITIAL_CAP", 0.0) or 0.0))


def _fresh_runtime_payload(balance: float, request_id: str) -> str:
    return json.dumps({
        "version": 1,
        "reason": f"paper_validation_reset:{request_id}"[:160],
        "balance": balance,
        "peak_balance": balance,
        "positions": [],
        "trade_ids": {},
        "cooldown": {},
    }, separators=(",", ":"), sort_keys=True)


def _fresh_wallet_payload(balance: float, request_id: str) -> str:
    return json.dumps({
        "version": 1,
        "balance": balance,
        "peak_balance": balance,
        "reason": f"paper_validation_reset:{request_id}"[:160],
    }, separators=(",", ":"), sort_keys=True)


def install(log):
    from bot import database as db
    from bot import durable_execution as durable

    if getattr(durable, "_paper_validation_reset_patched", False):
        return

    original_restore = durable.restore_engine_state

    async def restore_with_optional_paper_reset(engine):
        request_id = _requested_reset_id()
        if getattr(engine, "paper_trade", False) and request_id:
            try:
                already_applied = await db.load_key_value(_RESET_MARKER_KEY, strict=True)
                if already_applied != request_id:
                    raw_runtime = await db.load_key_value(durable.PAPER_STATE_KEY, strict=True)
                    if raw_runtime:
                        state = json.loads(raw_runtime)
                        positions = state.get("positions", []) if isinstance(state, dict) else None
                        if not isinstance(positions, list):
                            raise ValueError("invalid persisted PAPER positions")
                        if positions:
                            raise RuntimeError(
                                "PAPER reset refused: persisted simulated positions are still open"
                            )

                    balance = _initial_balance(log)
                    if balance <= 0:
                        raise RuntimeError(
                            "PAPER reset refused: PAPER_INITIAL_BALANCE/INITIAL_CAP must be > 0"
                        )

                    await db.save_key_value(
                        _WALLET_KEY,
                        _fresh_wallet_payload(balance, request_id),
                        strict=True,
                    )
                    await db.save_key_value(
                        durable.PAPER_STATE_KEY,
                        _fresh_runtime_payload(balance, request_id),
                        strict=True,
                    )
                    await db.save_key_value(_RESET_MARKER_KEY, request_id, strict=True)
                    log.warning(
                        "[PAPER_RESET] validation state reset id=%s balance=$%.4f; "
                        "LIVE/exchange state untouched",
                        request_id, balance,
                    )
                    _direct_observe(
                        f"state=APPLIED balance={balance:.4f} peak={balance:.4f} positions=0"
                    )
                else:
                    log.info("[PAPER_RESET] request id=%s already applied; skipping", request_id)
                    raw_wallet = await db.load_key_value(_WALLET_KEY, strict=True)
                    raw_runtime = await db.load_key_value(durable.PAPER_STATE_KEY, strict=True)
                    wallet = json.loads(raw_wallet) if raw_wallet else {}
                    runtime = json.loads(raw_runtime) if raw_runtime else {}
                    balance = float(wallet.get("balance", runtime.get("balance", 0.0)) or 0.0)
                    peak = float(wallet.get("peak_balance", runtime.get("peak_balance", 0.0)) or 0.0)
                    positions = runtime.get("positions", []) if isinstance(runtime, dict) else []
                    position_count = len(positions) if isinstance(positions, list) else -1
                    _direct_observe(
                        f"state=ALREADY_APPLIED balance={balance:.4f} peak={peak:.4f} positions={position_count}"
                    )
            except Exception as exc:
                log.critical(
                    "[PAPER_RESET] reset failed closed; new PAPER entries remain protected: %s: %s",
                    type(exc).__name__, exc,
                )
                _direct_observe(f"state=FAILED error={type(exc).__name__}")
                durable._block(engine, "paper_reset")
                return False

        result = await original_restore(engine)
        if request_id and getattr(engine, "paper_trade", False):
            durable._clear(engine, "paper_reset")
        return result

    durable.restore_engine_state = restore_with_optional_paper_reset
    durable._paper_validation_reset_patched = True
