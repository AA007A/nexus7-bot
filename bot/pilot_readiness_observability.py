"""Non-sensitive startup observability for controlled LIVE pilot readiness.

This module is diagnostic only. It does not change PAPER/LIVE selection,
release authorization, exchange connectivity, risk limits, or order routing.
It intentionally reports booleans only and never logs release-token values or
credentials.
"""
from __future__ import annotations

import os


def snapshot() -> dict:
    from bot import pilot

    paper = os.environ.get("PAPER_TRADE", "true").strip().lower() == "true"
    configured = bool(pilot.PILOT_ENABLED)
    release_approved = bool(pilot._release_approved())
    return {
        "configured": configured,
        "enabled": configured and not paper,
        "paper_trade": paper,
        "release_approved": release_approved,
    }


def install(log) -> None:
    state = snapshot()
    log.info(
        "[PILOT_READINESS] configured=%s enabled=%s paper_trade=%s release_approved=%s",
        str(state["configured"]).lower(),
        str(state["enabled"]).lower(),
        str(state["paper_trade"]).lower(),
        str(state["release_approved"]).lower(),
    )
