"""Production hardening wrapper for the NEXUS-7 API.

Keeps the legacy ``main:app`` module intact while adding controls that are
safe to deploy independently:

* fail-closed score-threshold alignment before the trading app is imported;
* a real readiness endpoint suitable for Railway health checks;
* an explicit confirmation header for destructive emergency close-all calls;
* audit logging for destructive API attempts.

This module does not change trading thresholds, position sizing, leverage,
order routing, or exchange credentials.
"""
from __future__ import annotations

import os
import time

from fastapi import Request
from fastapi.responses import JSONResponse


# ---------------------------------------------------------------------------
# Threshold contract: strategy candidate floor and NEXUS evaluation floor
# must not silently diverge in production. If NEXUS_MIN_SCORE is absent, use
# the strategy floor. If both are present but differ, fail closed at startup.
# The canonical code fallback is 60, matching production's current contract.
# ---------------------------------------------------------------------------
_strategy_floor_raw = os.environ.get("MIN_ENTRY_SCORE", "60").strip() or "60"
_nexus_floor_raw = os.environ.get("NEXUS_MIN_SCORE", _strategy_floor_raw).strip() or _strategy_floor_raw
try:
    _strategy_floor = float(_strategy_floor_raw)
    _nexus_floor = float(_nexus_floor_raw)
except ValueError as exc:
    raise RuntimeError("invalid score threshold configuration") from exc

if _strategy_floor != _nexus_floor:
    raise RuntimeError(
        "score threshold drift: "
        f"MIN_ENTRY_SCORE={_strategy_floor:g} != NEXUS_MIN_SCORE={_nexus_floor:g}"
    )
os.environ.setdefault("NEXUS_MIN_SCORE", _strategy_floor_raw)

# Console-script launchers may add the app directory only after Python's
# automatic sitecustomize discovery. Import it explicitly before composing
# the engine. Normal imports are cached: failed installations are not retried
# or marked healthy, and the existing startup classifier still fails closed.
import sitecustomize  # noqa: E402, F401

# Import only after the threshold contract has been validated.
from main import app  # noqa: E402
from bot.kucoin import PAPER_TRADE, TRADING_MODE_REASON  # noqa: E402
from bot import runtime_mode_observability as runtime_mode  # noqa: E402
from bot.logger import log  # noqa: E402


@app.middleware("http")
async def destructive_admin_guard(request: Request, call_next):
    """Require explicit human intent for the destructive close-all endpoint.

    Bearer authentication and the existing rate limiter remain enforced by the
    original endpoint. This adds a separate confirmation gate against
    accidental/cross-client invocation. It intentionally does not weaken or
    bypass the existing authentication layer.
    """
    if request.method.upper() == "POST" and request.url.path == "/api/close-all":
        confirm = request.headers.get("X-Confirm-Action", "")
        if confirm != "CLOSE_ALL_POSITIONS":
            log.warning(
                "[ADMIN_GUARD] action=close_all result=BLOCKED reason=confirmation_missing "
                "client=%s execution_effect=NONE",
                request.client.host if request.client else "unknown",
            )
            return JSONResponse(
                status_code=428,
                content={
                    "detail": "Explicit confirmation required",
                    "required_header": "X-Confirm-Action: CLOSE_ALL_POSITIONS",
                },
            )
        log.critical(
            "[ADMIN_GUARD] action=close_all result=CONFIRMED client=%s ts=%d",
            request.client.host if request.client else "unknown",
            int(time.time()),
        )
    return await call_next(request)


@app.get("/ready", include_in_schema=False)
async def readiness():
    """Operational readiness: HTTP 503 whenever trading runtime is not ready.

    Unlike /health (liveness), this endpoint is designed for Railway readiness
    checks. It performs no exchange I/O and derives state from the already
    observed runtime so a transient KuCoin call cannot itself make the probe
    hang.
    """
    engine = getattr(app.state, "engine", None)
    blocked = bool(getattr(app.state, "blocked", False))
    snap = runtime_mode.snapshot(
        paper_trade=PAPER_TRADE,
        engine=engine,
        blocked=blocked,
        mode_reason=TRADING_MODE_REASON,
    )
    durable_ok = bool(getattr(engine, "_durable_state_ok", False)) if engine else False
    instruments = len(getattr(engine, "instruments", {}) or {}) if engine else 0

    ready = bool(
        snap.get("ready")
        and snap.get("connected")
        and snap.get("active")
        and not blocked
        and durable_ok
        and instruments > 0
    )
    body = {
        "status": "ready" if ready else "not_ready",
        "ready": ready,
        "connected": bool(snap.get("connected")),
        "active": bool(snap.get("active")),
        "blocked": blocked,
        "durable_state_ok": durable_ok,
        "instruments": instruments,
        "trading_mode": snap.get("trading_mode"),
        "validation_lock": snap.get("validation_lock"),
        "orders_sent_to_exchange": snap.get("orders_sent_to_exchange"),
        "score_floor": _strategy_floor,
    }
    return JSONResponse(status_code=200 if ready else 503, content=body)
