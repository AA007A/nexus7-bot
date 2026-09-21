"""Typed fail-closed API for state required to open new financial risk.

Telemetry may remain best-effort. New-risk authorization must not.
Risk-reducing exchange actions deliberately do not depend on this repository.
"""
from __future__ import annotations
from bot import database as db


class CriticalStateUnavailable(db.PersistenceError):
    pass


class CriticalStateRepository:
    async def load(self, key: str):
        try:
            return await db.load_key_value(key, strict=True)
        except Exception as exc:
            raise CriticalStateUnavailable(f"critical state read unavailable: {key}") from exc

    async def save(self, key: str, value) -> None:
        try:
            ok = await db.save_key_value(key, value, strict=True)
            if ok is not True:
                raise CriticalStateUnavailable(f"critical state write unconfirmed: {key}")
        except CriticalStateUnavailable:
            raise
        except Exception as exc:
            raise CriticalStateUnavailable(f"critical state write unavailable: {key}") from exc

    def assert_available_for_new_risk(self) -> None:
        if getattr(db, "_conn", None) is None or db.configured_postgres_unavailable():
            raise CriticalStateUnavailable("critical database unavailable for OPEN_NEW_RISK")


critical_state = CriticalStateRepository()
