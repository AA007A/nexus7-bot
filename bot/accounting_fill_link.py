"""Read-only execution evidence. Never updates trading/risk accounting.

KuCoin Classic /api/v1/fills uses contract sizes and nanosecond tradeTime.
Only an isolated, balanced position with known opening order IDs and matching
history prices/fees is attributed. Missing evidence remains explicitly pending.
"""
from decimal import Decimal


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('nonfinite execution value')
    return result


def symbol(value):
    return str(value).removesuffix('M').replace('XBT', 'BTC')


async def fills(client, row):
    start, end = int(row['openTime']), int(row['closeTime'])
    if not 0 <= end - start <= 7 * 86400000:
        raise ValueError('unsupported fill window')
    found = {}
    expected = None
    for page in range(1, 11):
        data = await client._get('/api/v1/fills', params={
            'symbol': row['symbol'], 'startAt': start - 2000,
            'endAt': end + 2000, 'currentPage': page, 'pageSize': 100}, auth=True)
        if not isinstance(data, dict) or not isinstance(data.get('items'), list):
            raise ValueError('missing fills page')
        total = int(data.get('totalPage', -1))
        count = int(data.get('totalNum', -1))
        if int(data.get('currentPage', 0)) != page or not 0 <= total <= 10 or count < 0:
            raise ValueError('invalid fills coverage')
        if expected is None:
            expected = (total, count)
        if expected != (total, count):
            raise ValueError('fills changed during pagination')
        for item in data['items']:
            safe = {k: item[k] for k in ('tradeId', 'orderId', 'symbol', 'side',
                    'size', 'price', 'fee', 'feeCurrency', 'tradeTime', 'tradeType')}
            token = str(safe['tradeId'])
            if not token or not safe['orderId']:
                raise ValueError('missing fill identity')
            if token in found and found[token] != safe:
                raise ValueError('conflicting fill identity')
            found[token] = safe
        if page >= total:
            if len(found) != count:
                raise ValueError('incomplete fills')
            return sorted(found.values(), key=lambda f: (int(f['tradeTime']), str(f['tradeId'])))
    raise ValueError('fills exceed budget')


async def reconcile(client, row, history, registry):
    result = dict(ownership='UNATTRIBUTED', fills_reconciled=False,
                  reconciliation_version=1, reconciliation_reason='PENDING_EVIDENCE')
    try:
        start, end = int(row['openTime']), int(row['closeTime'])
        # Adjacent/reversed account positions cannot safely share boundary fills.
        for other in history:
            if str(other['closeId']) != str(row['closeId']) and other.get('symbol') == row['symbol']:
                if int(other['openTime']) <= end + 4000 and int(other['closeTime']) >= start - 4000:
                    result['reconciliation_reason'] = 'OVERLAPPING_POSITION_HISTORY'
                    return result
        orders = {}
        for order in registry:
            if order.get('state') == 'FILLED' and str(order.get('client_oid', '')).startswith('bgx7-') and order.get('order_id'):
                token = str(order['order_id'])
                if token in orders:
                    raise ValueError('duplicate registry order')
                orders[token] = order
        if not any(symbol(o.get('symbol')) == symbol(row['symbol']) for o in orders.values()):
            result['reconciliation_reason'] = 'NO_DURABLE_BOT_ORDER'
            return result
        executions = await fills(client, row)
        result['fills'] = executions
        if not executions:
            raise ValueError('no fills')
        direction = {'LONG': 'buy', 'SHORT': 'sell'}[row['side']]
        opening, closing = [], []
        balance = Decimal(0)
        for i, fill in enumerate(executions):
            timestamp = int(fill['tradeTime']) // 1000000
            if not start - 2000 <= timestamp <= end + 2000 or fill['symbol'] != row['symbol']:
                raise ValueError('fill outside position window')
            if fill['feeCurrency'] != row['settleCurrency'] or fill['tradeType'] != 'trade':
                raise ValueError('unsupported fill accounting')
            size, price = number(fill['size']), number(fill['price'])
            if size <= 0 or price <= 0 or fill['side'] not in ('buy', 'sell'):
                raise ValueError('invalid execution')
            if fill['side'] == direction:
                order = orders.get(str(fill['orderId']))
                if order is None or symbol(order.get('symbol')) != symbol(row['symbol']) or str(order.get('side')).lower() != direction:
                    result['reconciliation_reason'] = 'EXTERNAL_OR_UNKNOWN_OPENING_ORDER'
                    return result
                opening.append(fill)
                balance += size
            else:
                closing.append(fill)
                balance -= size
            if balance < 0 or (balance == 0 and i != len(executions) - 1):
                raise ValueError('multiple or reversed positions')
        if balance != 0 or not opening or not closing:
            raise ValueError('unbalanced executions')
        if abs(int(executions[0]['tradeTime']) // 1000000 - start) > 2000 or abs(int(executions[-1]['tradeTime']) // 1000000 - end) > 2000:
            raise ValueError('position boundary mismatch')
        for subset, field in ((opening, 'openPrice'), (closing, 'closePrice')):
            qty = sum(number(f['size']) for f in subset)
            average = sum(number(f['size']) * number(f['price']) for f in subset) / qty
            if abs(average - number(row[field])) > Decimal('0.00000001'):
                raise ValueError('history price mismatch')
        fees = sum(number(f['fee']) for f in executions)
        if abs(fees - number(row['tradeFee'])) > Decimal('0.00000001'):
            raise ValueError('history fee mismatch')
        result.update(ownership='BGX_ORDER_IDS', fills_reconciled=True,
                      reconciliation_reason='MATCHED_IDS_QUANTITIES_PRICES_FEES',
                      opening_order_ids=sorted({str(f['orderId']) for f in opening}),
                      closing_order_ids=sorted({str(f['orderId']) for f in closing}),
                      contracts=str(sum(number(f['size']) for f in opening)),
                      fill_fees=str(fees))
    except Exception as exc:
        result['reconciliation_reason'] = 'UNCONFIRMED_' + type(exc).__name__
    return result
