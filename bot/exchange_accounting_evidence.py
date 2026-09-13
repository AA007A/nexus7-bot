"""Passive, durable exchange position-history evidence; never trading authority.

Source: KuCoin Classic Futures GET /api/v1/history-positions. Confirmed BGX fills
are joined with the durable entry lineage when available, producing one
exchange-authoritative post-trade accounting record without affecting execution.
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
        data = await client._get('/api/v1/history-positions', params={'from': start_ms, 'to': end_ms, 'limit': 100, 'pageId': page}, auth=True)
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


async def _load_lineage(row):
    from bot.durable_daily_stop import state_key
    raw = await db.load_key_value(state_key('trade_lineage') + ':' + str(row['symbol']), strict=True)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get('version') != 1 or str(value.get('symbol')) != str(row['symbol']):
        return None
    try:
        entry = float(value.get('entry', 0))
        exchange_entry = float(row.get('openPrice', 0))
    except (TypeError, ValueError):
        return None
    tolerance = max(abs(exchange_entry) * 0.005, 1e-12)
    if entry <= 0 or exchange_entry <= 0 or abs(entry - exchange_entry) > tolerance:
        return None
    return value


async def audit(engine):
    from bot.durable_daily_stop import state_key
    end = int(time.time() * 1000)
    start = end - 48 * 3600000
    try:
        rows = await asyncio.wait_for(collect(engine.client, start, end), timeout=20)
        from bot.accounting_fill_link import reconcile
        from bot.durable_execution import _ORDER_KEY
        raw_registry = await db.load_key_value(_ORDER_KEY, strict=True)
        registry = json.loads(raw_registry) if raw_registry else {'version': 1, 'orders': []}
        if registry.get('version') != 1 or not isinstance(registry.get('orders'), list):
            raise ValueError('invalid durable order registry')
        deadline = time.monotonic() + 60
        for row in rows:
            key = state_key('accounting') + ':' + hashlib.sha256(str(row['closeId']).encode()).hexdigest()[:24]
            receipt = dict(row, source='KUCOIN_POSITION_HISTORY', ownership='UNATTRIBUTED', fills_reconciled=False)
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                receipt.update(await asyncio.wait_for(reconcile(engine.client, row, rows, registry['orders']), timeout=min(15, remaining)))
            except asyncio.TimeoutError:
                receipt.update(reconciliation_version=1, reconciliation_reason='FILL_QUERY_TIMEOUT')

            if receipt.get('ownership') == 'BGX_ORDER_IDS' and receipt.get('fills_reconciled') is True:
                lineage = await _load_lineage(row)
                if lineage is not None:
                    receipt['lineage'] = lineage
                    receipt['lineage_reconciled'] = True
                else:
                    receipt['lineage_reconciled'] = False

            encoded = json.dumps(receipt, sort_keys=True, separators=(',', ':'))
            if await db.load_key_value(key, strict=True) != encoded:
                if await db.save_key_value(key, encoded, strict=True) is not True:
                    raise db.PersistenceError('history persistence unconfirmed')
                log.info('[EXCHANGE_ACCOUNTING_EVIDENCE] symbol=%s close_id=%s exchange_pnl=%s trade_fee=%s funding_fee=%s open_time=%s close_time=%s open_price=%s close_price=%s currency=%s source=KUCOIN_POSITION_HISTORY ownership=%s fills_reconciled=%s durable=true execution_effect=NONE', row.get('symbol', 'NA'), row['closeId'], row.get('pnl', 'NA'), row.get('tradeFee', 'NA'), row.get('fundingFee', 'NA'), row.get('openTime', 'NA'), row.get('closeTime', 'NA'), row.get('openPrice', 'NA'), row.get('closePrice', 'NA'), row.get('settleCurrency', 'NA'), receipt['ownership'], receipt['fills_reconciled'])
                log.info('[EXCHANGE_FILL_LINK] close_id=%s ownership=%s fills_reconciled=%s reason=%s opening_order_ids=%s closing_order_ids=%s durable=true execution_effect=NONE', row['closeId'], receipt['ownership'], receipt['fills_reconciled'], receipt['reconciliation_reason'], receipt.get('opening_order_ids', []), receipt.get('closing_order_ids', []))

            if receipt.get('ownership') == 'BGX_ORDER_IDS' and receipt.get('fills_reconciled') is True:
                lineage = receipt.get('lineage') or {}
                log.warning('[POST_TRADE_ACCOUNTING_CONFIRMED] symbol=%s close_id=%s accounting_source=KUCOIN_RECONCILED_FILLS fills_confirmed=true exchange_pnl=%s trade_fee=%s funding_fee=%s open_price=%s close_price=%s contracts=%s nexus=%s regime=%s entry_type=%s score=%s lineage_reconciled=%s decision_effect=NONE execution_effect=NONE', row.get('symbol', 'NA'), row['closeId'], row.get('pnl', 'NA'), row.get('tradeFee', 'NA'), row.get('fundingFee', 'NA'), row.get('openPrice', 'NA'), row.get('closePrice', 'NA'), receipt.get('contracts', 'NA'), lineage.get('nexus', 'UNKNOWN'), lineage.get('regime', 'UNKNOWN'), lineage.get('entry_type', 'UNKNOWN'), lineage.get('score', 'NA'), receipt.get('lineage_reconciled', False))
        log.info('[EXCHANGE_ACCOUNTING_COVERAGE] start_ms=%s end_ms=%s positions=%s complete=true scope=POSITION_HISTORY execution_effect=NONE', start, end, len(rows))
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
