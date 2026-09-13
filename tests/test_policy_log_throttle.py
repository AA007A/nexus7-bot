import unittest
from unittest.mock import Mock, patch

from bot.policy_log_throttle import DrawdownAdvisoryLogProxy, wrap


class PolicyLogThrottleTests(unittest.TestCase):
    def test_repeated_drawdown_warning_is_throttled(self):
        delegate = Mock()
        proxy = DrawdownAdvisoryLogProxy(delegate, interval_s=300)
        with patch('bot.policy_log_throttle.time.monotonic', side_effect=[100.0, 101.0, 401.0]):
            proxy.warning('[DRAWDOWN_ADVISORY] drawdown=%s', 0.4)
            proxy.warning('[DRAWDOWN_ADVISORY] drawdown=%s', 0.41)
            proxy.warning('[DRAWDOWN_ADVISORY] drawdown=%s', 0.42)
        self.assertEqual(delegate.warning.call_count, 2)

    def test_non_drawdown_warning_is_never_throttled(self):
        delegate = Mock()
        proxy = DrawdownAdvisoryLogProxy(delegate, interval_s=300)
        proxy.warning('[PROTECTION_GUARD] problem')
        proxy.warning('[PROTECTION_GUARD] problem')
        self.assertEqual(delegate.warning.call_count, 2)

    def test_distinct_drawdown_advisory_sources_have_independent_visibility(self):
        delegate = Mock()
        proxy = DrawdownAdvisoryLogProxy(delegate, interval_s=300)
        with patch('bot.policy_log_throttle.time.monotonic', return_value=100.0):
            proxy.warning('[DRAWDOWN_ADVISORY] drawdown=%s', 0.4)
            proxy.warning('[DRAWDOWN_ADVISORY_INSTANCE] drawdown=%s', 0.4)
        self.assertEqual(delegate.warning.call_count, 2)

    def test_wrap_is_idempotent_and_other_levels_delegate(self):
        delegate = Mock()
        proxy = wrap(delegate)
        self.assertIs(proxy, wrap(proxy))
        proxy.info('hello')
        proxy.critical('critical')
        delegate.info.assert_called_once_with('hello')
        delegate.critical.assert_called_once_with('critical')


if __name__ == '__main__':
    unittest.main()
