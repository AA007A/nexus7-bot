"""Passive, durable exchange position-history evidence; never trading authority.

Source: KuCoin Classic Futures GET /api/v1/history-positions. Reported PnL,
trade fees and funding are retained verbatim. BGX attribution requires durable
bot order IDs plus reconciled fills and exact opening-order lineage. External
classification requires positive exchange order identity proving that opening
orders do not use the BGX clientOid namespace. Missing/ambiguous evidence stays
UNKNOWN_UNATTRIBUTED and can never authorize execution.
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


async def _load_lineage_for_opening_orders(opening_order_ids, row):
    from bot.post_trade_forensics import _lineage_key
    if not isinstance(opening_order_ids, list) or len(opening_order_ids) != 1:
        return None
    order_id = str(opening_order_ids[0] or '')
    if not order_id:
        return None
    raw = await db.load_key_value(_lineage_key(order_id), strict=True)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get('version') != 2:
        return None
    if str(value.get('order_id') or '') != order_id:
        return None
    symbol = str(row.get('symbol') or '')
    lineage_symbol = str(value.get('symbol') or '')
    if lineage_symbol not in (symbol, symbol.removesuffix('M')):
        return None
    try:
        captured_ms = int(value.get('captured_at_ms', 0) or 0)
        order_created_ms = int(value.get('order_created_at_ms', 0) or 0)
        open_ms = int(row.get('openTime', 0) or 0)
    except (TypeError, ValueError):
        return None
    if not captured_ms or not order_created_ms or not open_ms:
        return None
    if abs(order_created_ms - open_ms) > 120000:
        return None
    if captured_ms + 120000 < open_ms:
        return None
    return value


def _opening_fill_order_ids(receipt, row):
    executions = receipt.get('fills')
    if not isinstance(executions, list) or not executions:
        return []
    try:
        direction = {'LONG': 'buy', 'SHORT': 'sell'}[str(row.get('side'))]
    except KeyError:
        return []
    result = []
    seen = set()
    for fill in executions:
        if not isinstance(fill, dict) or str(fill.get('side')).lower() != direction:
            continue
        order_id = str(fill.get('orderId') or '')
        if order_id and order_id not in seen:
            seen.add(order_id)
            result.append(order_id)
    return result


async def _classify_origin(client, receipt, registry, row):
    """Return a telemetry-only origin class; uncertainty always stays UNKNOWN."""
    if (receipt.get('ownership') == 'BGX_ORDER_IDS'
            and receipt.get('fills_reconciled') is True
            and receipt.get('lineage_reconciled') is True):
        return 'BGX_CONFIRMED', 'DURABLE_IDS_FILLS_AND_LINEAGE'

    opening_ids = _opening_fill_order_ids(receipt, row)
    if not opening_ids:
        return 'UNKNOWN_UNATTRIBUTED', 'NO_COMPLETE_OPENING_FILL_IDENTITY'

    durable = {
        str(order.get('order_id')): order for order in registry
        if isinstance(order, dict) and order.get('order_id')
    }
    if any(order_id in durable for order_id in opening_ids):
        return 'UNKNOWN_UNATTRIBUTED', 'DURABLE_ORDER_PRESENT_WITHOUT_FULL_BGX_PROOF'

    expected_symbol = str(row.get('symbol') or '')
    expected_side = {'LONG': 'buy', 'SHORT': 'sell'}.get(str(row.get('side')))
    if not expected_symbol or not expected_side:
        return 'UNKNOWN_UNATTRIBUTED', 'INVALID_POSITION_IDENTITY'

    for order_id in opening_ids:
        try:
            detail = await asyncio.wait_for(
                client._get(f'/api/v1/orders/{order_id}', auth=True), timeout=5
            )
        except Exception:
            return 'UNKNOWN_UNATTRIBUTED', 'ORDER_IDENTITY_LOOKUP_UNCONFIRMED'
        if not isinstance(detail, dict):
            return 'UNKNOWN_UNATTRIBUTED', 'ORDER_IDENTITY_INVALID'
        returned_id = str(detail.get('id') or detail.get('orderId') or '')
        if returned_id != order_id:
            return 'UNKNOWN_UNATTRIBUTED', 'ORDER_IDENTITY_MISMATCH'
        if str(detail.get('symbol') or '') != expected_symbol:
            return 'UNKNOWN_UNATTRIBUTED', 'ORDER_SYMBOL_MISMATCH'
        if str(detail.get('side') or '').lower() != expected_side:
            return 'UNKNOWN_UNATTRIBUTED', 'ORDER_SIDE_MISMATCH'
        client_oid = detail.get('clientOid')
        if not isinstance(client_oid, str) or not client_oid:
            return 'UNKNOWN_UNATTRIBUTED', 'CLIENT_OID_MISSING'
        if client_oid.startswith('bgx7-'):
            return 'UNKNOWN_UNATTRIBUTED', 'BGX_CLIENT_OID_WITHOUT_DURABLE_PROOF'

    # Positive proof that every observed opening order belongs outside the BGX
    # clientOid namespace. This means external/non-BGX; it does not prove which
    # human or external automation submitted the order.
    return 'MANUAL_EXTERNAL', 'NON_BGX_CLIENT_OID_CONFIRMED'


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
            receipt = dict(row, source='KUCOIN_POSITION_HISTORY',
                           ownership='UNATTRIBUTED', fills_reconciled=False)
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                receipt.update(await asyncio.wait_for(
                    reconcile(engine.client, row, rows, registry['orders']), timeout=min(15, remaining)))
            except asyncio.TimeoutError:
                receipt.update(reconciliation_version=1, reconciliation_reason='FILL_QUERY_TIMEOUT')

            if receipt.get('ownership') == 'BGX_ORDER_IDS' and receipt.get('fills_reconciled') is True:
                lineage = await _load_lineage_for_opening_orders(
                    receipt.get('opening_order_ids', []), row
                )
                if lineage is not None:
                    receipt['lineage'] = lineage
                    receipt['lineage_reconciled'] = True
                else:
                    receipt['lineage_reconciled'] = False

            origin_class, origin_reason = await _classify_origin(
                engine.client, receipt, registry['orders'], row
            )
            receipt['origin_class'] = origin_class
            receipt['origin_reason'] = origin_reason

            encoded = json.dumps(receipt, sort_keys=True, separators=(',', ':'))
            previous = await db.load_key_value(key, strict=True)
            if previous == encoded:
                continue
            if await db.save_key_value(key, encoded, strict=True) is not True:
                raise db.PersistenceError('history persistence unconfirmed')

            log.info('[EXCHANGE_ACCOUNTING_EVIDENCE] symbol=%s close_id=%s exchange_pnl=%s trade_fee=%s funding_fee=%s open_time=%s close_time=%s open_price=%s close_price=%s currency=%s source=KUCOIN_POSITION_HISTORY ownership=%s fills_reconciled=%s origin_class=%s origin_reason=%s durable=true execution_effect=NONE',
                     row.get('symbol', 'NA'), row['closeId'], row.get('pnl', 'NA'),
                     row.get('tradeFee', 'NA'), row.get('fundingFee', 'NA'),
                     row.get('openTime', 'NA'), row.get('closeTime', 'NA'),
                     row.get('openPrice', 'NA'), row.get('closePrice', 'NA'), row.get('settleCurrency', 'NA'),
                     receipt['ownership'], receipt['fills_reconciled'], receipt['origin_class'], receipt['origin_reason'])
            log.info('[EXCHANGE_FILL_LINK] close_id=%s ownership=%s fills_reconciled=%s reason=%s opening_order_ids=%s closing_order_ids=%s origin_class=%s durable=true execution_effect=NONE',
                     row['closeId'], receipt['ownership'], receipt['fills_reconciled'],
                     receipt.get('reconciliation_reason', 'UNKNOWN'), receipt.get('opening_order_ids', []),
                     receipt.get('closing_order_ids', []), receipt['origin_class'])

            if receipt.get('origin_class') == 'BGX_CONFIRMED':
                lineage = receipt.get('lineage') or {}
                log.warning('[POST_TRADE_ACCOUNTING_CONFIRMED] symbol=%s close_id=%s accounting_source=KUCOIN_RECONCILED_FILLS fills_confirmed=true exchange_pnl=%s trade_fee=%s funding_fee=%s open_price=%s close_price=%s opening_order_ids=%s closing_order_ids=%s nexus=%s regime=%s entry_type=%s score=%s lineage_reconciled=%s origin_class=BGX_CONFIRMED decision_effect=NONE execution_effect=NONE',
                            row.get('symbol', 'NA'), row['closeId'], row.get('pnl', 'NA'),
                            row.get('tradeFee', 'NA'), row.get('fundingFee', 'NA'),
                            row.get('openPrice', 'NA'), row.get('closePrice', 'NA'),
                            receipt.get('opening_order_ids', []), receipt.get('closing_order_ids', []),
                            lineage.get('nexus', 'UNKNOWN'), lineage.get('regime', 'UNKNOWN'),
                            lineage.get('entry_type', 'UNKNOWN'), lineage.get('score', 'NA'),
                            receipt.get('lineage_reconciled', False))
            elif receipt.get('origin_class') == 'MANUAL_EXTERNAL':
                log.info('[POST_TRADE_EXTERNAL_CONFIRMED] symbol=%s close_id=%s origin_class=MANUAL_EXTERNAL evidence=NON_BGX_CLIENT_OID bot_will_not_claim_trade=true decision_effect=NONE execution_effect=NONE',
                         row.get('symbol', 'NA'), row['closeId'])
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
