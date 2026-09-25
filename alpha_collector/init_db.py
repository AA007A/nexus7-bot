"""Deployment-only bootstrap for Phase 8H BGX-RESEARCH database authority.

Creates/migrates only the alpha_prospective schema and initializes the frozen
BGX_RESEARCH_PROSPECTIVE_ALPHA_V1 authority row. No market collection or trading.
"""
from __future__ import annotations

import asyncio
import os

from alpha_collector import collector as K
from alpha_collector import db as D


async def _main() -> None:
    env = os.environ
    g = K.env_guard(env)
    if g["mode"] != "PREFLIGHT":
        raise D.Refused("deployment bootstrap is allowed only in PREFLIGHT")
    store = await D.PgStore.connect(
        env.get("PROSPECTIVE_DB_URL", ""),
        production_fingerprint=env.get("PRODUCTION_DB_FINGERPRINT"),
    )
    try:
        await store.migrate()
        await store.init_authority()
        await store.verify_authority()
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(_main())
