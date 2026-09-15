"""Atomic multi-key durable persistence for critical key_value state.

This module intentionally performs one database transaction for a group of
key/value updates. It does not change execution authorization, strategy, risk,
leverage, sizing, or exchange state.
"""
from __future__ import annotations

from bot import database as db
from bot.logger import log


async def save_key_values_atomic(items, *, strict: bool = False) -> bool:
    """Persist all ``items`` or none of them.

    ``items`` is an iterable of ``(key, value)`` pairs. PostgreSQL uses one
    asyncpg transaction; SQLite uses BEGIN IMMEDIATE plus explicit commit/
    rollback. The database module's shared I/O lock serializes this transaction
    with the rest of the process' durable-state operations.
    """
    pairs = [(str(key), str(value)) for key, value in items]
    if not pairs:
        raise ValueError("atomic key/value write requires at least one item")
    if any(not key for key, _ in pairs):
        raise ValueError("atomic key/value keys must be non-empty")
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("atomic key/value keys must be unique")

    conn = db._conn
    if not conn:
        if strict:
            raise db.PersistenceError("atomic key/value write: database unavailable")
        return False

    async with db._io_lock:
        try:
            ts = db._now()
            if db._is_pg:
                async with conn.transaction():
                    for key, value in pairs:
                        await conn.execute(
                            "INSERT INTO key_value (key,value,updated_at) VALUES ($1,$2,$3) "
                            "ON CONFLICT (key) DO UPDATE SET value=$2,updated_at=$3",
                            key, value, ts,
                        )
            else:
                await conn.execute("BEGIN IMMEDIATE")
                for key, value in pairs:
                    await conn.execute(
                        "INSERT OR REPLACE INTO key_value (key,value,updated_at) VALUES (?,?,?)",
                        (key, value, ts),
                    )
                await conn.commit()
            return True
        except Exception as exc:
            if not db._is_pg:
                try:
                    await conn.rollback()
                except Exception as rollback_exc:
                    log.error("atomic key/value rollback failed: %s", rollback_exc)
            log.error("atomic key/value write failed: %s", exc)
            if strict:
                raise db.PersistenceError("atomic key/value write failed") from exc
            return False
