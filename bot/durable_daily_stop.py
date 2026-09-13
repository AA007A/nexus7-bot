"""Persist only evidence-backed LIVE daily stops before the scanner can run.

Version 3 retires the v2 namespace because a bare in-memory ``daily_stopped``
flag could be persisted as ``STOP`` without recording what PnL breached which
limit.  A fresh stop now requires confirmed durable daily-PnL state plus current
realized+unrealized PnL at or below the configured loss limit.  Once a valid v3
stop is persisted it remains latched for the UTC day, preserving circuit-breaker
semantics across restarts.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timezone

from bot import database as db
from bot import durable_daily_pnl
from bot.logger import log


STATE_VERSION = 3
_STATE_SOURCE = "RUNTIME_DAILY_STOP"


def state_key(day):
    # Deliberately excludes deployment/commit: a deploy is not a new day.
    # v3 retires the evidence-free v2 namespace without weakening future
    # persistence of legitimately triggered daily stops.
    scope = '|'.join(os.environ.get(k, '') for k in (
        'RAILWAY_PROJECT_ID', 'RAILWAY_SERVICE_ID', 'RAILWAY_ENVIRONMENT_ID'))
    return f'daily_stop_v{STATE_VERSION}:' + hashlib.sha256(scope.encode()).hexdigest()[:24] + ':' + day


def _finite(value, default=None):
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _stop_limit(engine):
    direct = _finite(getattr(engine, 'daily_stop_loss', None))
    if direct is not None and direct > 0:
        return direct
    tracker = getattr(engine, 'daily_tracker', None)
    tracked = _finite(getattr(tracker, 'daily_stop_loss', None)) if tracker is not None else None
    return tracked if tracked is not None and tracked > 0 else None


def _unrealized(engine):
    total = 0.0
    for position in getattr(engine, 'positions', {}).values():
        pnl = _finite(getattr(position, 'pnl', 0.0), 0.0)
        total += pnl
    return total


def _clear_unproven_flag(engine):
    engine.daily_stopped = False
    tracker = getattr(engine, 'daily_tracker', None)
    if tracker is not None:
        tracker.daily_stopped = False


def _decode_state(raw, day):
    if raw is None:
        return None
    try:
        state = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise db.PersistenceError('invalid daily stop json') from exc
    if not isinstance(state, dict):
        raise db.PersistenceError('invalid daily stop object')
    if state.get('version') != STATE_VERSION or state.get('day') != day:
        raise db.PersistenceError('invalid daily stop version/day')
    if state.get('source') != _STATE_SOURCE:
        raise db.PersistenceError('invalid daily stop source')
    trigger_pnl = _finite(state.get('trigger_pnl'))
    stop_limit = _finite(state.get('stop_limit'))
    triggered_at = state.get('triggered_at')
    if trigger_pnl is None or stop_limit is None or stop_limit <= 0:
        raise db.PersistenceError('invalid daily stop evidence')
    if trigger_pnl > -stop_limit + 1e-12:
        raise db.PersistenceError('daily stop evidence does not prove breach')
    try:
        stamp = datetime.fromisoformat(str(triggered_at))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if stamp.astimezone(timezone.utc).date().isoformat() != day:
            raise ValueError('trigger day mismatch')
    except (TypeError, ValueError) as exc:
        raise db.PersistenceError('invalid daily stop timestamp') from exc
    return state


def _encode_state(day, combined_pnl, stop_limit, now):
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return json.dumps({
        'version': STATE_VERSION,
        'day': day,
        'trigger_pnl': float(combined_pnl),
        'stop_limit': float(stop_limit),
        'triggered_at': stamp.astimezone(timezone.utc).isoformat(),
        'source': _STATE_SOURCE,
    }, sort_keys=True, separators=(',', ':'), allow_nan=False)


async def entries_blocked(engine, now=None):
    if getattr(engine, 'paper_trade', False) or getattr(engine, '_validation_safety_lock_active', False):
        return False
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    day = instant.astimezone(timezone.utc).date().isoformat()
    key = state_key(day)
    try:
        if db.configured_postgres_unavailable():
            raise db.PersistenceError('configured PostgreSQL unavailable')
        if os.environ.get('RAILWAY_SERVICE_ID') and not db._is_pg and db.SQLITE_PATH.startswith('/tmp/'):
            raise db.PersistenceError('ephemeral SQLite cannot preserve a deployment stop')

        stored = await db.load_key_value(key, strict=True)
        state = _decode_state(stored, day)
        if state is not None:
            engine.daily_stopped = True
            tracker = getattr(engine, 'daily_tracker', None)
            if tracker is not None:
                tracker.daily_stopped = True
            if getattr(engine, '_daily_stop_restored_key', None) != key:
                log.warning(
                    '[DURABLE_DAILY_STOP] day=%s state=RESTORED entries_blocked=true '
                    'trigger_pnl=%s stop_limit=%s evidence=v3 protection_unchanged=true',
                    day, state['trigger_pnl'], state['stop_limit'])
                engine._daily_stop_restored_key = key
            return True

        if not getattr(engine, 'daily_stopped', False):
            return False

        # A fresh durable STOP cannot be manufactured from a boolean.  The
        # durable ledger must already be confirmed for this runtime iteration.
        if getattr(engine, '_daily_pnl_ok', False) is not True:
            log.error(
                '[DURABLE_DAILY_STOP] day=%s state=TRIGGER_UNCONFIRMED '
                'entries_blocked=true reason=daily_pnl_unconfirmed', day)
            return True

        stop_limit = _stop_limit(engine)
        if stop_limit is None:
            log.error(
                '[DURABLE_DAILY_STOP] day=%s state=TRIGGER_UNCONFIRMED '
                'entries_blocked=true reason=invalid_stop_limit', day)
            return True

        stats = getattr(engine, 'stats', None)
        if stats is None:
            log.error(
                '[DURABLE_DAILY_STOP] day=%s state=TRIGGER_UNCONFIRMED '
                'entries_blocked=true reason=missing_stats', day)
            return True

        realized = float(durable_daily_pnl.realized(stats, instant))
        unrealized = float(_unrealized(engine))
        combined = realized + unrealized

        if combined > -stop_limit + 1e-12:
            # The boolean is stale/unproven.  Clearing only this flag restores
            # entry eligibility; all other risk/execution protections remain.
            _clear_unproven_flag(engine)
            log.warning(
                '[DURABLE_DAILY_STOP] day=%s state=UNPROVEN_FLAG_CLEARED '
                'entries_blocked=false realized=%s unrealized=%s combined=%s '
                'stop_limit=%s protection_unchanged=true',
                day, realized, unrealized, combined, stop_limit)
            return False

        encoded = _encode_state(day, combined, stop_limit, instant)
        if await db.save_key_value(key, encoded, strict=True) is not True:
            raise db.PersistenceError('daily stop write unconfirmed')
        log.warning(
            '[DURABLE_DAILY_STOP] day=%s state=PERSISTED entries_blocked=true '
            'trigger_pnl=%s stop_limit=%s evidence=v3', day, combined, stop_limit)
        return True
    except Exception as exc:
        # Storage or state-integrity failure remains fail-closed.  It does not
        # fabricate a loss event or mutate the durable stop record.
        log.error('[DURABLE_DAILY_STOP] state=UNCONFIRMED entries_blocked=true error=%s', type(exc).__name__)
        return True
