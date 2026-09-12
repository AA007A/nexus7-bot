"""Exercise source handoffs with the real runtime cache and mocked HTTP."""
import asyncio
import time
from unittest.mock import patch

from bot import market_risk_runtime as runtime
from bot import market_risk_binance_fallback as fallback


class Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Log:
    def info(self, *args):
        pass

    def warning(self, *args):
        pass


def collect(rate):
    async def fetch(session, url):
        if 'premiumIndex' in url:
            return 200, {'lastFundingRate': str(rate), 'time': time.time() * 1000}
        return 200, []

    with patch.object(fallback.aiohttp, 'ClientSession', Session), patch.object(fallback, '_fetch_json', fetch):
        asyncio.run(fallback._collect_once(runtime, Log()))


def test_fallback_refreshes_its_own_fresh_cache_on_next_poll():
    with patch.object(runtime, '_state', {}):
        collect(0.0001)
        assert runtime.snapshot()['signals']['funding_rate_pct'] == 0.01
        collect(0.0008)
        snap = runtime.snapshot()
        assert snap['signals']['funding_rate_pct'] == 0.08
        assert snap['signal_providers']['funding_rate_pct'] == fallback._PROVIDER
        assert snap['providers'][fallback._PROVIDER].startswith('ok_fallback:')


def test_primary_recovery_takes_priority_over_fallback():
    with patch.object(runtime, '_state', {}):
        collect(0.0001)
        runtime._merge_signals({'funding_rate_pct': 0.04}, provider='coinglass_v4')
        collect(0.0008)
        snap = runtime.snapshot()
        assert snap['signals']['funding_rate_pct'] == 0.04
        assert snap['signal_providers']['funding_rate_pct'] == 'coinglass_v4'


def test_stale_primary_is_replaced_by_fresh_fallback():
    with patch.object(runtime, '_state', {}):
        runtime._merge_signals({'funding_rate_pct': 0.04}, now=time.time()-3600, provider='coinglass_v4')
        collect(0.0008)
        assert runtime.snapshot()['signals']['funding_rate_pct'] == 0.08


def test_empty_primary_response_does_not_claim_fallback_data():
    with patch.object(runtime, '_state', {}):
        collect(0.0001)
        runtime._merge_signals({}, provider='coinglass_v4')
        collect(0.0008)
        assert runtime.snapshot()['signal_providers']['funding_rate_pct'] == fallback._PROVIDER
        assert runtime.snapshot()['signals']['funding_rate_pct'] == 0.08


def test_merge_without_provider_clears_previous_attribution():
    with patch.object(runtime, '_state', {}):
        collect(0.0001)
        runtime._merge_signals({'funding_rate_pct': 0.04})
        assert runtime.snapshot()['signal_providers']['funding_rate_pct'] == 'unknown'
        assert not fallback._field_needs_fallback(runtime, 'funding_rate_pct')


def test_source_age_still_expires_repeatedly_fetched_old_observation():
    with patch.object(runtime, '_state', {}), patch.dict(runtime._SOURCE_MAX_AGE_S, {'funding_rate_pct': 7200}):
        runtime._merge_signals({'funding_rate_pct': 0.04}, observed_at=time.time()-8000, provider=fallback._PROVIDER)
        assert 'funding_rate_pct' not in runtime.snapshot()['signals']
        assert 'funding_rate_pct' not in runtime.snapshot()['signal_providers']
