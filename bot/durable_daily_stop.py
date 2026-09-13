"""Persist only triggered LIVE daily stops, before the scanner can run.

Version 2 intentionally ignores the legacy v1 namespace because v1 contained a
one-off hardcoded migration that could fabricate a STOP for 2026-09-13. Real
daily stops are still persisted fail-closed once the runtime tracker actually
sets ``engine.daily_stopped``.
"""
import hashlib
import os
from datetime import datetime, timezone

from bot import database as db
from bot.logger import log


STATE_VERSION = 2


def state_key(day):
    # Deliberately excludes deployment/commit: a deploy is not a new day.
    # v2 retires the contaminated v1 namespace without weakening future
    # persistence of legitimately triggered daily stops.
    scope = '|'.join(os.environ.get(k, '') for k in (
        'RAILWAY_PROJECT_ID', 'RAILWAY_SERVICE_ID', 'RAILWAY_ENVIRONMENT_ID'))
    return f'daily_stop_v{STATE_VERSION}:' + hashlib.sha256(scope.encode()).hexdigest()[:24] + ':' + day


async def entries_blocked(engine, now=None):
    if getattr(engine, 'paper_trade', False) or getattr(engine, '_validation_safety_lock_active', False):
        return False
    day = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()
    key = state_key(day)
    try:
        if db.configured_postgres_unavailable():
            raise db.PersistenceError('configured PostgreSQL unavailable')
        if os.environ.get('RAILWAY_SERVICE_ID') and not db._is_pg and db.SQLITE_PATH.startswith('/tmp/'):
            raise db.PersistenceError('ephemeral SQLite cannot preserve a deployment stop')
        stored = await db.load_key_value(key, strict=True)
        if stored not in (None, 'STOP'):
            raise db.PersistenceError('invalid daily stop state')
        if stored == 'STOP':
            engine.daily_stopped = True
            if getattr(engine, 'daily_tracker', None) is not None:
                engine.daily_tracker.daily_stopped = True
            if getattr(engine, '_daily_stop_restored_key', None) != key:
                log.warning('[DURABLE_DAILY_STOP] day=%s state=RESTORED entries_blocked=true protection_unchanged=true', day)
                engine._daily_stop_restored_key = key
            return True
        if getattr(engine, 'daily_stopped', False):
            if await db.save_key_value(key, 'STOP', strict=True) is not True:
                raise db.PersistenceError('daily stop write unconfirmed')
            log.warning('[DURABLE_DAILY_STOP] day=%s state=PERSISTED entries_blocked=true', day)
            return True
        return False
    except Exception as exc:
        # Do not turn storage failure into a fabricated daily loss or stop.
        # The main loop keeps managing existing positions before this gate.
        log.error('[DURABLE_DAILY_STOP] state=UNCONFIRMED entries_blocked=true error=%s', type(exc).__name__)
        return True
