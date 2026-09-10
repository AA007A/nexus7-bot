"""Fail-closed staged connector for SHADOW LIVE validation.

This module exists only for read-only validation. It never grants execution
permission and never performs exchange mutations. Each startup stage is logged
separately so a live KuCoin payload/runtime incompatibility cannot collapse into
an opaque ``ValueError``.
"""
from __future__ import annotations

import asyncio

from bot.logger import log
from bot import shadow_balance_semantics as balance_semantics
from bot.account_capital_reader import read_account_capital
from bot.risk_manager_v3 import RiskManagerV3


def _shadow_risk_v3(engine) -> RiskManagerV3:
    risk = getattr(engine, "_shadow_risk_v3", None)
    if not isinstance(risk, RiskManagerV3):
        risk = RiskManagerV3()
        engine._shadow_risk_v3 = risk
    return risk


def _mark_disconnected(engine) -> None:
    engine.connected = False
    engine.active = False


def _stage_error(stage: str, exc: Exception) -> None:
    # Keep diagnostics useful without dumping credentials/headers into logs.
    detail = str(exc).replace("\n", " ")[:180]
    log.error(
        "[SHADOW_CONNECT] stage=%s result=BLOCK error=%s detail=%s execution_effect=NONE",
        stage,
        type(exc).__name__,
        detail or "n/a",
    )


async def connect_readonly(engine) -> bool:
    """Connect SHADOW LIVE with explicit, fail-closed startup stages."""
    _mark_disconnected(engine)

    try:
        ping_ok = await engine.client.ping()
        log.info(
            "[SHADOW_CONNECT] stage=PING result=%s execution_effect=NONE",
            "PASS" if ping_ok else "DEGRADED",
        )
        if not ping_ok:
            log.warning(
                "[SHADOW_LIVE] exchange ping failed; continuing with REST read-only checks"
            )
    except Exception as exc:
        # Ping is advisory; authenticated REST reads below are authoritative.
        _stage_error("PING", exc)

    try:
        state = await balance_semantics.refresh_shadow_risk(engine)
        equity = float(state["equity"])
        available = float(state["available"])
        if equity < 0 or available < 0:
            raise ValueError("negative equity/available balance")
        log.info(
            "[SHADOW_CONNECT] stage=BALANCE result=PASS equity=%.4f available=%.4f execution_effect=NONE",
            equity,
            available,
        )
    except Exception as exc:
        _stage_error("BALANCE", exc)
        return False

    try:
        capital_snapshot = await read_account_capital(engine.client)
        _shadow_risk_v3(engine).update_capital(capital_snapshot.capital)
        log.info(
            "[SHADOW_CONNECT] stage=CAPITAL_V3 result=PASS equity=%.4f available=%.4f execution_effect=NONE",
            capital_snapshot.capital.equity,
            capital_snapshot.capital.available_collateral,
        )
    except Exception as exc:
        _stage_error("CAPITAL_V3", exc)
        return False

    try:
        await engine.client.load_instruments()
        engine.instruments = engine.client.get_instruments()
        if not isinstance(engine.instruments, dict) or not engine.instruments:
            raise ValueError("instrument metadata unavailable")
        log.info(
            "[SHADOW_CONNECT] stage=INSTRUMENTS result=PASS count=%s execution_effect=NONE",
            len(engine.instruments),
        )
    except Exception as exc:
        _stage_error("INSTRUMENTS", exc)
        return False

    try:
        viable_ok = await engine._filter_viable_symbols()
        # Do not trust a boolean alone: the runtime contract requires a
        # non-empty explicit symbol list after validation.
        if viable_ok is not True or not engine.viable_symbols:
            raise ValueError("no validated viable symbols")
        unknown = [s for s in engine.viable_symbols if s not in engine.instruments]
        if unknown:
            raise ValueError("viable symbols missing instrument metadata")
        log.info(
            "[SHADOW_CONNECT] stage=VIABILITY result=PASS count=%s execution_effect=NONE",
            len(engine.viable_symbols),
        )
    except Exception as exc:
        engine.viable_symbols = []
        _stage_error("VIABILITY", exc)
        return False

    try:
        await asyncio.wait_for(
            engine.client.start_websocket(
                engine.viable_symbols[:30], intervals=["15", "60", "240"]
            ),
            timeout=10,
        )
        log.info(
            "[SHADOW_CONNECT] stage=PUBLIC_WS result=PASS execution_effect=NONE"
        )
    except asyncio.TimeoutError:
        # REST remains authoritative for SHADOW; timeout is degraded, not fatal.
        log.warning(
            "[SHADOW_CONNECT] stage=PUBLIC_WS result=DEGRADED reason=timeout REST_fallback=true execution_effect=NONE"
        )
    except Exception as exc:
        # Same policy as the previous implementation: public WS failure does not
        # authorize execution and SHADOW may continue with REST fallbacks.
        _stage_error("PUBLIC_WS", exc)
        log.warning(
            "[SHADOW_CONNECT] stage=PUBLIC_WS fallback=REST execution_effect=NONE"
        )

    engine.connected = True
    engine.active = True
    log.warning(
        "[SHADOW_LIVE] CORE_READY balance_read=true capital_v3=true viable_symbols=%s "
        "private_ws=pending execution_effect=NONE",
        len(engine.viable_symbols),
    )
    return True
