"""Persist operational realized PnL; never infer trades from account balances.

Stores the engine's existing net PnL, including explicitly marked estimates.
This is not a replacement for exchange fill reconciliation or performance stats.
"""
import asyncio
import hashlib
import json
import math
import os
from datetime import datetime, timezone

from bot import database as db
from bot.logger import log


def utc_day(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).date().isoformat()


def event(trade):
    pnl = float(trade.pnl)
    if not math.isfinite(pnl):
        raise ValueError('nonfinite realized PnL')
    # Do not round timestamps/quantities: repeated reads of a trade must match,
    # but distinct fills/closures must not be collapsed by a coarse time bucket.
    identity = [trade.symbol, trade.direction, trade.opened_at.isoformat(),
                trade.closed_at.isoformat(), str(trade.qty), str(trade.entry),
                str(trade.exit_price)]
    token = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return token, {'pnl': pnl, 'source': getattr(trade, 'accounting_source',
                   'ENGINE_OPERATIONAL_NET_PNL'), 'closed_at': trade.closed_at.isoformat()}


def realized(stats, now=None):
    day = utc_day(now or datetime.now(timezone.utc))
    rows = dict(getattr(stats, '_durable_daily_pnl', {}).get(day, {}))
    for trade in stats.trades:
        if utc_day(trade.closed_at) == day:
            key, value = event(trade)
            rows[key] = value
    return math.fsum(float(value['pnl']) for value in rows.values())


async def checkpoint(engine, extra=None, now=None):
    if getattr(engine, 'paper_trade', False) or getattr(engine, '_validation_safety_lock_active', False):
        return True
    if not getattr(engine, '_durable_state_enforced', False):
        return True
    if not hasattr(engine, '_daily_pnl_lock'):
        engine._daily_pnl_lock = asyncio.Lock()
    async with engine._daily_pnl_lock:
        try:
            if db.configured_postgres_unavailable():
                raise db.PersistenceError('configured PostgreSQL unavailable')
            if os.environ.get('RAILWAY_SERVICE_ID') and not db._is_pg and db.SQLITE_PATH.startswith('/tmp/'):
                raise db.PersistenceError('ephemeral daily PnL storage')
            day = utc_day(now or datetime.now(timezone.utc))
            from bot.durable_daily_stop import state_key
            key = state_key('pnl-ledger-v1:' + day)
            raw = await db.load_key_value(key, strict=True)
            state = json.loads(raw) if raw is not None else {
                'version': 1, 'day': day, 'events': {},
                'coverage_started_at': (now or datetime.now(timezone.utc)).isoformat()}
            if state.get('version') != 1 or state.get('day') != day or not isinstance(state.get('events'), dict):
                raise ValueError('invalid daily PnL ledger')
            rows = state['events']
            for token, value in rows.items():
                if not isinstance(token, str) or len(token) != 64 or not isinstance(value, dict):
                    raise ValueError('invalid daily PnL event')
                if not math.isfinite(float(value['pnl'])) or utc_day(datetime.fromisoformat(value['closed_at'])) != day:
                    raise ValueError('invalid daily PnL value')
            for trade in list(engine.stats.trades) + ([extra] if extra is not None else []):
                if utc_day(trade.closed_at) != day:
                    continue
                token, value = event(trade)
                if token in rows and rows[token] != value:
                    raise ValueError('conflicting daily PnL event')
                rows[token] = value
            encoded = json.dumps(state, sort_keys=True, separators=(',', ':'), allow_nan=False)
            if encoded != raw:
                if await db.save_key_value(key, encoded, strict=True) is not True:
                    raise db.PersistenceError('daily PnL write unconfirmed')
            engine.stats._durable_daily_pnl = {day: dict(rows)}
            engine._daily_pnl_ok = True
            fingerprint = (day, len(rows), math.fsum(float(v['pnl']) for v in rows.values()))
            if getattr(engine, '_daily_pnl_logged', None) != fingerprint:
                log.info('[DURABLE_DAILY_PNL] day=%s events=%s realized_net=%s durable=true source=ENGINE_OPERATIONAL history_backfill=false coverage_started_at=%s',
                         *fingerprint, state.get('coverage_started_at', 'UNCONFIRMED'))
                engine._daily_pnl_logged = fingerprint
            return True
        except Exception as exc:
            engine._daily_pnl_ok = False
            log.error('[DURABLE_DAILY_PNL] state=UNCONFIRMED entries_blocked=true error=%s', type(exc).__name__)
            return False
