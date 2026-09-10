"""Durable fail-closed submission cap for the controlled LIVE pilot.

The in-memory PilotGuard counter is useful for fast diagnostics, but a process
restart must not reset the two-order safety budget for the same Railway
deployment. This module adds an authoritative durable reservation immediately
before KuCoin network dispatch.

It never closes, reduces, adopts, or modifies positions. reduceOnly exits are
explicitly excluded from the new-entry budget.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone

from bot.logger import log

_LOCK = asyncio.Lock()
_STATE_VERSION = 1


def _session_id() -> str:
    """Stable across process restarts of one deployment; changes on a deploy."""
    raw = (
        os.environ.get("PILOT_SESSION_ID")
        or os.environ.get("RAILWAY_DEPLOYMENT_ID")
        or os.environ.get("RAILWAY_GIT_COMMIT_SHA")
        or "local"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _state_key() -> str:
    return f"pilot_submission_budget_v1:{_session_id()}"


def _order_token(symbol: str, side: str, qty, idem_key: str | None) -> str:
    raw = f"{symbol}|{side}|{qty}|{idem_key or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _decode(value) -> list[str]:
    if value is None:
        return []
    try:
        obj = json.loads(str(value))
        if not isinstance(obj, dict) or obj.get("version") != _STATE_VERSION:
            raise ValueError("invalid durable pilot state")
        ids = obj.get("order_tokens")
        if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids):
            raise ValueError("invalid durable pilot order token list")
        return list(dict.fromkeys(ids))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("durable pilot state malformed") from exc


def _encode(tokens: list[str]) -> str:
    return json.dumps(
        {
            "version": _STATE_VERSION,
            "order_tokens": tokens,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


async def _reserve_db(token: str, limit: int) -> tuple[bool, int]:
    """Atomically reserve token in PostgreSQL/SQLite key_value storage."""
    from bot import database as db

    if limit <= 0:
        raise RuntimeError("invalid pilot submission limit")
    conn = getattr(db, "_conn", None)
    if conn is None:
        raise db.PersistenceError("pilot reservation blocked: database unavailable")
    if db.configured_postgres_unavailable():
        raise db.PersistenceError("pilot reservation blocked: PostgreSQL unavailable")

    key = _state_key()
    async with _LOCK:
        # _io_lock protects the shared connection from concurrent repository I/O.
        async with db._io_lock:
            if getattr(db, "_is_pg", False):
                async with conn.transaction():
                    row = await conn.fetchrow(
                        "SELECT value FROM key_value WHERE key=$1 FOR UPDATE", key
                    )
                    tokens = _decode(row[0] if row else None)
                    if token in tokens:
                        return True, len(tokens)
                    if len(tokens) >= limit:
                        return False, len(tokens)
                    tokens.append(token)
                    payload = _encode(tokens)
                    ts = datetime.now(timezone.utc).isoformat()
                    await conn.execute(
                        "INSERT INTO key_value (key,value,updated_at) VALUES ($1,$2,$3) "
                        "ON CONFLICT (key) DO UPDATE SET value=$2,updated_at=$3",
                        key, payload, ts,
                    )
                    return True, len(tokens)

            await conn.execute("BEGIN IMMEDIATE")
            try:
                async with conn.execute(
                    "SELECT value FROM key_value WHERE key=?", (key,)
                ) as cur:
                    row = await cur.fetchone()
                tokens = _decode(row[0] if row else None)
                if token in tokens:
                    await conn.commit()
                    return True, len(tokens)
                if len(tokens) >= limit:
                    await conn.commit()
                    return False, len(tokens)
                tokens.append(token)
                payload = _encode(tokens)
                ts = datetime.now(timezone.utc).isoformat()
                await conn.execute(
                    "INSERT OR REPLACE INTO key_value (key,value,updated_at) VALUES (?,?,?)",
                    (key, payload, ts),
                )
                await conn.commit()
                return True, len(tokens)
            except Exception:
                await conn.rollback()
                raise


async def reserve_submission(
    symbol: str, side: str, qty, idem_key: str | None, limit: int
) -> tuple[bool, int]:
    token = _order_token(symbol, side, qty, idem_key)
    return await _reserve_db(token, limit)


def install(KuCoinClient, log_obj=log) -> None:
    """Install a pre-dispatch durable pilot reservation around place_order."""
    if getattr(KuCoinClient, "_pilot_durable_submission_counter_installed", False):
        return

    original = KuCoinClient.place_order

    async def place_order_with_durable_pilot_budget(self, *args, **kwargs):
        reduce_only = bool(kwargs.get("reduce_only", False))
        single_submission = bool(kwargs.get("single_submission", False))
        if reduce_only or not single_submission:
            return await original(self, *args, **kwargs)

        from bot.pilot import MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION

        symbol = kwargs.get("symbol") or (args[0] if len(args) > 0 else "")
        side = kwargs.get("side") or (args[1] if len(args) > 1 else "")
        qty = kwargs.get("qty") if "qty" in kwargs else (args[2] if len(args) > 2 else 0)
        idem_key = kwargs.get("idem_key")
        try:
            allowed, count = await reserve_submission(
                str(symbol), str(side), qty, idem_key,
                MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION,
            )
        except Exception as exc:
            log_obj.critical(
                "[PILOT_DURABLE_COUNTER] symbol=%s result=BLOCK error=%s "
                "exchange_dispatch=NONE",
                symbol, type(exc).__name__,
            )
            return {}

        if not allowed:
            log_obj.critical(
                "[PILOT_DURABLE_COUNTER] symbol=%s result=BLOCK reserved=%s/%s "
                "exchange_dispatch=NONE",
                symbol, count, MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION,
            )
            return {}

        log_obj.critical(
            "[PILOT_DURABLE_COUNTER] symbol=%s result=RESERVED reserved=%s/%s",
            symbol, count, MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION,
        )
        return await original(self, *args, **kwargs)

    KuCoinClient.place_order = place_order_with_durable_pilot_budget
    KuCoinClient._pilot_durable_submission_counter_installed = True
    log_obj.info(
        "[PILOT_DURABLE_COUNTER] installed: two-order budget survives process "
        "restart within the same deployment; fail-closed before dispatch"
    )
