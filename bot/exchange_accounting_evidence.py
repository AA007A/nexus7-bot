"""Passive, durable exchange position-history evidence; never trading authority.

Source: KuCoin Classic Futures GET /api/v1/history-positions.
Reported pnl/tradeFee/fundingFee are retained verbatim: their signs are not
reinterpreted and account history is never implicitly attributed to BGX.
"""
import asyncio
import hashlib
import json
import time

from bot import database as db
from bot.logger import log

FIELDS = ('closeId', 'symbol', 'settleCurrency', 'type', 'side', 'pnl',
          'realisedGrossCost', 'realisedGrossCostNew', 'tradeFee', 'fundingFee',
          'tax', 'openTime', 'closeTime', 'openPrice', 'closePrice', 'leverage')


async def collect(client, start_ms, end_ms):
    if not 0 < end_ms - start_ms <= 7 * 86400000:
        raise ValueError('invalid history window')
    rows = {}
    for page in range(1, 21):
        data = await client._get('/api/v1/history-positions',
            params={'from': start_ms, 'to': end_ms, 'limit': 100, 'pageId': page}, auth=True)
        if not isinstance(data, dict) or not isinstance(data.get('items'), list):
            raise ValueError('history response unconfirmed')
        if int(data.get('currentPage', 0)) != page:
            raise ValueError('history page mismatch')
        total = int(data.get('totalPage', -1))
        if total < 0 or total > 20:
            raise ValueError('history coverage exceeds budget')
        for row in data['items']:
            if not isinstance(row, dict) or not row.get('closeId'):
                raise ValueError('history missing close identifier')
            timestamp = int(row.get('closeTime', 0))
            if not start_ms <= timestamp <= end_ms:
                raise ValueError('history row outside requested window')
            safe = {key: row[key] for key in FIELDS if key in row}
            token = str(row['closeId'])
            if token in rows and rows[token] != safe:
                raise ValueError('conflicting duplicate history')
            rows[token] = safe
        if page >= total:
            return list(rows.values())
    raise ValueError('history incomplete')


async def audit(engine):
    from bot.durable_daily_stop import state_key
    end = int(time.time() * 1000)
    start = end - 48 * 3600000
    try:
        rows = await asyncio.wait_for(collect(engine.client, start, end), timeout=20)
        for row in rows:
            # Scope storage to the same service/environment as daily risk state.
            key = state_key('accounting') + ':' + hashlib.sha256(str(row['closeId']).encode()).hexdigest()[:24]
            receipt = dict(row, source='KUCOIN_POSITION_HISTORY',
                           ownership='UNATTRIBUTED', fills_reconciled=False)
            encoded = json.dumps(receipt, sort_keys=True, separators=(',', ':'))
            if await db.load_key_value(key, strict=True) == encoded:
                continue
            if await db.save_key_value(key, encoded, strict=True) is not True:
                raise db.PersistenceError('history persistence unconfirmed')
            log.info('[EXCHANGE_ACCOUNTING_EVIDENCE] symbol=%s close_id=%s exchange_pnl=%s trade_fee=%s funding_fee=%s open_time=%s close_time=%s open_price=%s close_price=%s currency=%s source=KUCOIN_POSITION_HISTORY ownership=UNATTRIBUTED fills_reconciled=false durable=true execution_effect=NONE',
                     row.get('symbol', 'NA'), row['closeId'], row.get('pnl', 'NA'),
                     row.get('tradeFee', 'NA'), row.get('fundingFee', 'NA'),
                     row.get('openTime', 'NA'), row.get('closeTime', 'NA'),
                     row.get('openPrice', 'NA'), row.get('closePrice', 'NA'), row.get('settleCurrency', 'NA'))
        log.info('[EXCHANGE_ACCOUNTING_COVERAGE] start_ms=%s end_ms=%s positions=%s complete=true ownership=UNATTRIBUTED execution_effect=NONE', start, end, len(rows))
    except Exception as exc:
        log.warning('[EXCHANGE_ACCOUNTING_COVERAGE] complete=false result=UNCONFIRMED error=%s execution_effect=NONE', type(exc).__name__)


def schedule(engine):
    if getattr(engine, 'paper_trade', False) or getattr(engine, '_validation_safety_lock_active', False):
        return
    task = getattr(engine, '_accounting_evidence_task', None)
    if task is not None and not task.done():
        return
    now = time.monotonic()
    if now < getattr(engine, '_accounting_evidence_next', 0):
        return
    engine._accounting_evidence_next = now + 600
    engine._accounting_evidence_task = asyncio.create_task(audit(engine))
