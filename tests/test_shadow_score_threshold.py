import inspect

from bot import validation_safety_lock as lock


def test_shadow_threshold_is_60():
    assert lock.SHADOW_MIN_ENTRY_SCORE == 60


def test_shadow_threshold_is_scoped_to_validation_lock():
    source = inspect.getsource(lock.install)
    assert 'getattr(self, "paper_trade", False)' in source
    assert 'getattr(self, "_validation_safety_lock_active", False)' in source
    assert 'return SHADOW_MIN_ENTRY_SCORE' in source
    assert 'return original_effective_score(self)' in source


def test_threshold_change_does_not_add_exchange_mutations():
    source = inspect.getsource(lock)
    forbidden = (
        '.place_order(',
        '.cancel_order(',
        '.cancel_all_orders(',
        '.close_position(',
        '.set_leverage(',
        '.set_sl(',
        '.set_position_stops(',
    )
    for marker in forbidden:
        assert marker not in source
